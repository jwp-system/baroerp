"""데모 준비(기획안 9-1 데모 시나리오).

    uv run python -m app.demo files [폴더]
        시연용 엑셀: 거래처·품목·홈택스 세금계산서(매출·매입)
    uv run python -m app.demo seed [--months 3]
        빈 DB에 데모 사업장 + 데모 계정(DEMO_ACCOUNTS) + 최근 3개월 거래 이력
    uv run python -m app.demo reset
        모든 데이터를 지우고 처음 상태로 다시 채움(데모 사이트는 매일 새벽 4시 자동)
    python -m app.demo ensure
        컨테이너 기동 시: DEMO_MODE이고 DB가 비어 있으면 seed, 아니면 아무것도 하지 않음
        (GCP 데모는 컨테이너 안 SQLite라 인스턴스가 새로 뜰 때마다 처음 상태로 채운다)

시연 금액은 SCENARIO에 고정해 두고, 세금계산서 파일이 그 금액·사업자번호와 맞도록 만든다.
"""

import argparse
import io
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, Item, Partner, Role, User, Warehouse
from app.models.finance import PaymentKind, PaymentMethod, PaymentStatus
from app.models.inventory import DisposalReason
from app.models.user import ROLE_LABELS
from app.models.voucher import VoucherKind
from app.security import hash_password
from app.services import closing, inventory, payments
from app.services.excel_import import ITEM_COLUMNS, PARTNER_COLUMNS, import_items, import_partners
from app.services.master import seed_business_defaults
from app.services.voucher import LineInput, save_voucher
from app.timeutil import today

BUSINESS = {"name": "바로상사", "biz_no": "1234567815"}

# 데모 사이트 계정(소개 페이지에 공개). 대표는 간편 비밀번호도 정해 둔다
DEMO_PASSWORD = "12345678"
DEMO_PIN = "142536"
DEMO_ACCOUNTS = {  # 키: 소개 페이지의 '둘러보기' 주소(/demo/enter/<키>)
    "owner": ("박사장", "demo1@example.com", Role.OWNER),
    "sales": ("김과장", "demo2@example.com", Role.SALES),
    "purchasing": ("장대리", "demo3@example.com", Role.PURCHASING),
    "accountant": ("강부장", "demo4@example.com", Role.ACCOUNTANT),
}

# (거래처명, 구분, 사업자번호, 대표자, 업태, 종목, 결제조건(일))
PARTNERS = [
    ("맑은샘물", "매입", "1048145782", "김샘물", "제조", "먹는샘물", 30),
    ("한빛식품", "매입", "2148612303", "이한빛", "도매", "가공식품", 15),
    ("푸른농산", "매입", "1298145326", "박농부", "도소매", "농산물", 7),
    ("행복마트", "매출", "2208162517", "최행복", "소매", "슈퍼마켓", 30),
    ("동네편의점", "매출", "1078635503", "정동네", "소매", "편의점", 15),
    ("바다횟집", "매출", "6058122754", "한바다", "음식점", "한식", 30),
    ("우리도매", "공통", "1138654728", "윤우리", "도소매", "식품", 30),
]

# (품목명, 품목그룹, 규격, 재고단위, 단위용량, 용량단위, 거래단위, 환산수량, 매입기본, 매출기본,
#  매입단가, 판매단가, 과세구분, 적정재고)
ITEMS = [
    ("생수 500mL", "상품 > 음료 > 생수", "500mL", "병", 500, "mL", "박스", 20, "박스", "병",
     300, 600, "과세", 100),
    ("생수 2L", "상품 > 음료 > 생수", "2L", "병", 2, "L", "박스", 6, "박스", "박스",
     700, 1300, "과세", 60),
    ("탄산수 500mL", "상품 > 음료 > 탄산", "500mL", "병", 500, "mL", "박스", 24, "박스", "병",
     450, 900, "과세", 48),
    ("보리차 1.5L", "상품 > 음료 > 차", "1.5L", "병", 1.5, "L", "박스", 12, "박스", "병",
     800, 1500, "과세", 24),
    ("컵라면", "상품 > 식품 > 면류", "110g", "개", None, None, "박스", 24, "박스", "개",
     650, 1200, "과세", 48),
    ("즉석밥", "상품 > 식품 > 즉석식품", "210g", "개", None, None, "박스", 36, "박스", "개",
     900, 1600, "과세", 36),
    ("쌀 10kg", "상품 > 농산물", "10kg", "개", 10, "kg", None, None, None, None,
     28000, 36000, "면세", 10),
    ("달걀 30구", "상품 > 농산물", "30구", "개", None, None, None, None, None, None,
     6500, 8900, "면세", 10),
]  # fmt: skip

# 시연 시나리오 3~7번 금액(데모 진행표와 같게 유지)
SCENARIO = {
    "purchase": {"partner": "맑은샘물", "item": "생수 500mL", "unit": "박스", "qty": 10,
                 "unit_price": 6000},  # 60,000 + 부가세 6,000 → 재고 200병 · 100L
    "sale": {"partner": "행복마트", "item": "생수 500mL", "unit": "병", "qty": 100,
             "unit_price": 600},  # 60,000 + 부가세 6,000
    "disposal": {"item": "생수 500mL", "qty": 5},
    "receipt": {"partner": "행복마트", "amount": 66000},
}  # fmt: skip


# ---------- 시연용 파일 ----------


def _xlsx(columns, rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append([c[0] for c in columns])
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def partners_xlsx() -> bytes:
    header = ["거래처명", "구분", "사업자번호", "대표자", "업태", "종목", "결제조건(일)"]
    assert all(h in [c[0] for c in PARTNER_COLUMNS] for h in header)
    return _xlsx([(h,) for h in header], PARTNERS)


def items_xlsx() -> bytes:
    header = ["품목명", "품목그룹", "규격", "재고단위", "단위용량", "용량단위", "거래단위",
              "환산수량", "매입기본단위", "매출기본단위", "매입단가", "판매단가", "과세구분",
              "적정재고"]  # fmt: skip
    assert all(h in [c[0] for c in ITEM_COLUMNS] for h in header)
    return _xlsx([(h,) for h in header], ITEMS)


# 홈택스 '전자(세금)계산서 목록조회' 엑셀처럼 제목 행 + 같은 이름의 '상호' 열 두 개
HOMETAX_HEADER = ["작성일자", "승인번호", "발급일자", "공급자사업자등록번호", "종사업장번호",
                  "상호", "대표자명", "공급받는자사업자등록번호", "종사업장번호", "상호",
                  "대표자명", "합계금액", "공급가액", "세액", "품목명"]  # fmt: skip


def _hometax_book(direction: str, rows: list[list]) -> bytes:
    """홈택스 '전자(세금)계산서 목록' 엑셀 모양(제목·빈 줄·머리글·행)"""
    wb = Workbook()
    ws = wb.active
    ws.append(
        ["매출 전자(세금)계산서 목록" if direction == "sales" else "매입 전자(세금)계산서 목록"]
    )
    ws.append([])
    ws.append(HOMETAX_HEADER)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _hometax_row(direction, d, no, us, them, supply, vat, item) -> list:
    supplier, buyer = (us, them) if direction == "sales" else (them, us)
    return [d.isoformat(), no, d.isoformat(), supplier[0], "", supplier[1], supplier[2],
            buyer[0], "", buyer[1], buyer[2], supply + vat, supply, vat, item]  # fmt: skip


def hometax_xlsx(direction: str, d: date) -> bytes:
    """시나리오 매출(direction='sales')·매입('purchase') 전표와 맞는 세금계산서 1건."""
    s = SCENARIO["sale" if direction == "sales" else "purchase"]
    supply = s["qty"] * s["unit_price"]
    partner = next(p for p in PARTNERS if p[0] == s["partner"])
    us = (BUSINESS["biz_no"], BUSINESS["name"], "대표")
    them = (partner[2], partner[0], partner[3])
    no = f"{d:%Y%m%d}{'4100' if direction == 'sales' else '4200'}0000{d:%d}01"
    return _hometax_book(direction, [_hometax_row(direction, d, no, us, them, supply,
                                                  supply // 10, s["item"])])  # fmt: skip


def hometax_last_month_xlsx(db: Session, business: Business, direction: str) -> bytes:
    """데모 DB의 지난달 전표로 만든 거래처별 월합계 세금계산서(올리면 전표와 자동 대사).
    데모 사이트 소개 페이지의 '세금계산서 대사' 체험용."""
    from app.services.tax_invoice import unmatched_vouchers

    end = today().replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    groups: dict[int, list] = {}
    for v in unmatched_vouchers(db, business.id, direction):
        if start <= v.voucher_date <= end and v.partner.biz_no:
            groups.setdefault(v.partner_id, []).append(v)
    owner = next(n for n, _, r in DEMO_ACCOUNTS.values() if r == Role.OWNER)
    us = (business.biz_no, business.name, owner)
    rows = []
    for vouchers in groups.values():
        partner = vouchers[0].partner
        them = (partner.biz_no, partner.name, partner.ceo_name or "")
        supply = int(sum(v.supply_amount for v in vouchers))
        vat = int(sum(v.vat_amount for v in vouchers))
        no = f"{end:%Y%m%d}{'4100' if direction == 'sales' else '4200'}{partner.id:08d}"
        item = f"{len(vouchers)}건 월합계"
        rows.append(_hometax_row(direction, end, no, us, them, supply, vat, item))
    return _hometax_book(direction, rows)


def write_files(folder: Path, d: date | None = None) -> list[Path]:
    d = d or today()
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "1_거래처.xlsx": partners_xlsx(),
        "2_품목.xlsx": items_xlsx(),
        "6_세금계산서_매출.xlsx": hometax_xlsx("sales", d),
        "6_세금계산서_매입.xlsx": hometax_xlsx("purchase", d),
    }
    paths = []
    for name, data in files.items():
        path = folder / name
        path.write_bytes(data)
        paths.append(path)
    return paths


# ---------- 거래 이력 ----------


def seed(db: Session, months: int = 3, rnd_seed: int = 42) -> Business:
    """빈 DB에 데모 사업장·계정을 만들고 최근 몇 달의 매입·매출·수금·지급·폐기를 채운다.
    지난달 이전은 마감해 둔다(이번 달·지난달은 열어 둠). 체험용으로 대표 승인을 기다리는
    지급과 폐기를 하나씩 남긴다."""
    if db.scalar(select(Business.id).limit(1)) is not None:
        raise SystemExit("이미 사업장이 있어요. 빈 DB에서 실행해 주세요")
    business = Business(name=BUSINESS["name"], biz_no=BUSINESS["biz_no"])
    db.add(business)
    db.flush()
    seed_business_defaults(db, business)
    password_hash = hash_password(DEMO_PASSWORD)  # scrypt는 느리므로 한 번만
    users = {
        key: User(
            business_id=business.id, email=email, name=name, role=role, password_hash=password_hash
        )  # fmt: skip
        for key, (name, email, role) in DEMO_ACCOUNTS.items()
    }
    users["owner"].pin_hash = hash_password(DEMO_PIN)
    db.add_all(users.values())
    db.flush()
    owner, buyer, seller, accountant = (
        users[k] for k in ("owner", "purchasing", "sales", "accountant")
    )
    db.info["user_id"] = owner.id
    for result in (
        import_partners(db, business, io.BytesIO(partners_xlsx())),
        import_items(db, business, io.BytesIO(items_xlsx())),
    ):
        if result.errors:
            raise SystemExit(f"데모 기준정보 오류: {result.errors}")
    db.add(Warehouse(business_id=business.id, name="2창고"))
    db.flush()

    rnd = random.Random(rnd_seed)
    items = list(db.scalars(select(Item).where(Item.business_id == business.id)))
    partners = list(db.scalars(select(Partner).where(Partner.business_id == business.id)))
    suppliers = [p for p in partners if p.kind in ("purchase", "both")]
    customers = [p for p in partners if p.kind in ("sales", "both")]

    def trade_unit(item: Item, attr: str) -> tuple[int, Decimal]:
        unit_id = getattr(item, attr) or item.stock_unit_id
        factor = next((u.factor for u in item.units if u.unit_id == unit_id), Decimal(1))
        return unit_id, factor

    end = today()
    start = end.replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    stock: dict[int, Decimal] = {i.id: Decimal(0) for i in items}
    d = start
    while d <= end:
        if d.weekday() == 0:  # 월요일 입고: 적정재고의 2~3배까지 채운다
            for supplier in suppliers:
                lines = []
                for item in rnd.sample(items, 3):
                    unit_id, factor = trade_unit(item, "purchase_unit_id")
                    target = (item.safety_stock or Decimal(20)) * rnd.choice([2, 3])
                    qty = max(Decimal(1), ((target - stock[item.id]) / factor).to_integral_value())
                    price = item.purchase_price * factor
                    lines.append(LineInput(item.id, unit_id, qty, price))
                    stock[item.id] += qty * factor
                save_voucher(db, business, buyer, VoucherKind.PURCHASE, None, d, supplier.id, lines)
        if d.weekday() < 6:  # 월~토 판매
            for customer in rnd.sample(customers, rnd.choice([1, 2])):
                lines = []
                for item in rnd.sample(items, rnd.choice([1, 2, 3])):
                    unit_id, factor = trade_unit(item, "sale_unit_id")
                    qty = Decimal(rnd.randint(1, 6) if factor > 1 else rnd.randint(5, 30))
                    if qty * factor > stock[item.id]:
                        continue
                    price = item.sale_price * factor
                    lines.append(LineInput(item.id, unit_id, qty, price))
                    stock[item.id] -= qty * factor
                if lines:
                    save_voucher(db, business, seller, VoucherKind.SALES, None, d, customer.id,
                                 lines)  # fmt: skip
        if d.day == 25:  # 매달 25일: 한 달 전까지의 외상 정리
            _settle(db, business, accountant, owner, d, rnd)
        if d.day == 20 and stock[items[0].id] >= 5:  # 매달 파손 폐기 1건
            wh = db.scalar(select(Warehouse).where(Warehouse.business_id == business.id,
                                                   Warehouse.is_default))  # fmt: skip
            disposal = inventory.create_disposal(db, business, buyer, d, items[0].id, wh.id,
                                                 Decimal(rnd.randint(2, 5)), DisposalReason.DAMAGED,
                                                 "운송 중 파손")  # fmt: skip
            inventory.approve_disposal(db, business, owner, disposal)
            stock[items[0].id] -= disposal.qty
        d += timedelta(days=1)

    # 지난달 이전은 마감
    last_open = (end.replace(day=1) - timedelta(days=1)).replace(day=1)
    m = start
    while m < last_open:
        closing.close_month(db, business.id, closing.year_month(m), owner.id)
        m = (m + timedelta(days=32)).replace(day=1)

    # 체험용 승인 대기: 기준금액 이상 지급(관리 등록) 1건, 파손 폐기(구매 등록) 1건
    payables = payments.balances(db, business.id, PaymentKind.DISBURSEMENT, end)
    top = max(payables, key=lambda b: b.balance, default=None)
    if top is not None and top.balance >= business.payment_approval_threshold:
        payments.save_payment(db, business, accountant, PaymentKind.DISBURSEMENT, end,
                              top.partner.id, top.balance, PaymentMethod.TRANSFER,
                              memo="이번 달 대금 지급")  # fmt: skip
    wh = db.scalar(select(Warehouse).where(Warehouse.business_id == business.id,
                                           Warehouse.is_default))  # fmt: skip
    if stock[items[1].id] >= 3:
        inventory.create_disposal(db, business, buyer, end, items[1].id, wh.id, Decimal(3),
                                  DisposalReason.DAMAGED, "입고 중 파손")  # fmt: skip
    db.commit()
    return business


def reset(months: int = 3) -> Business:
    """데모 사이트를 처음 상태로: 모든 테이블을 비우고 다시 채운다(DB 버전 정보는 유지)."""
    from app.db import SessionLocal, engine
    from app.models import Base

    with engine.begin() as conn:
        for t in reversed(Base.metadata.sorted_tables):
            conn.execute(t.delete())
    with SessionLocal() as db:
        return seed(db, months)


def ensure(months: int = 3) -> Business | None:
    """데모 모드에서 빈 DB면 채운다. 이미 사업장이 있거나 데모가 아니면 None."""
    from app.config import get_settings
    from app.db import SessionLocal

    if not get_settings().demo_mode:
        return None
    with SessionLocal() as db:
        if db.scalar(select(Business.id).limit(1)) is not None:
            return None
        return seed(db, months)


def _settle(
    db: Session, business: Business, accountant: User, owner: User, d: date, rnd: random.Random
) -> None:
    cutoff = d - timedelta(days=30)
    for kind in (PaymentKind.RECEIPT, PaymentKind.DISBURSEMENT):
        totals: dict[int, Decimal] = {}
        for v, open_amount in payments.open_vouchers(db, business.id, kind, None):
            if v.voucher_date <= cutoff:
                totals[v.partner_id] = totals.get(v.partner_id, Decimal(0)) + open_amount
        for partner_id, amount in totals.items():
            if rnd.random() < 0.15:  # 일부는 연체로 남겨 에이징에 보이게
                continue
            p = payments.save_payment(db, business, accountant, kind, d, partner_id, amount,
                                      PaymentMethod.TRANSFER, memo="월말 정산")  # fmt: skip
            if p.status == PaymentStatus.PENDING:
                payments.approve_payment(db, business, owner, p)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.demo", description="바로ERP 데모 준비")
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("files", help="시연용 엑셀 파일 만들기")
    f.add_argument("folder", nargs="?", default="data/demo")
    f.add_argument("--date", type=date.fromisoformat, help="세금계산서 작성일자(기본 오늘)")
    for name, help_text in (("seed", "빈 DB에 데모 사업장·계정·거래 이력 채우기"),
                            ("reset", "모든 데이터를 지우고 처음 상태로 다시 채우기"),
                            ("ensure", "데모 모드에서 빈 DB면 채우기(기동 시)")):  # fmt: skip
        sub.add_parser(name, help=help_text).add_argument("--months", type=int, default=3)
    args = parser.parse_args(argv)

    if args.cmd == "files":
        for path in write_files(Path(args.folder), args.date):
            print(path)
        return
    if args.cmd == "reset":
        business = reset(args.months)
    elif args.cmd == "ensure":
        if (business := ensure(args.months)) is None:
            return
    else:
        from app.db import SessionLocal

        with SessionLocal() as db:
            business = seed(db, args.months)
    print(f"'{business.name}' 데모 사업장을 만들었어요. 계정(비밀번호 {DEMO_PASSWORD}):")
    for name, email, role in DEMO_ACCOUNTS.values():
        print(f"  {name} {email} ({ROLE_LABELS[role]})")
    print(f"  대표 간편 비밀번호 {DEMO_PIN}")


if __name__ == "__main__":
    main()
