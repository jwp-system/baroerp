"""데모 시나리오(기획안 9-1) 전 과정을 화면 요청으로 따라가는 통합 테스트.
시연용 파일(app.demo)과 시연 금액(SCENARIO)이 실제 화면 흐름과 맞는지도 함께 확인한다."""

import shutil
from decimal import Decimal

from sqlalchemy import select

from app import demo
from app.config import get_settings
from app.models import Disposal, Item, Partner, TaxInvoice, Unit, Voucher, Warehouse
from app.services import backup
from app.services.voucher import stock_balances
from app.timeutil import today
from tests.conftest import csrf_of

PASSWORD = "demo-pass-1234"
PIN = "802461"


def _post(client, path, data, form_path=None, files=None):
    token = csrf_of(client, form_path or path)
    return client.post(path, data={**data, "csrf_token": token}, files=files)


def _numbers(db, business_id):
    """복원 전후로 비교할 전표·재고 수치"""
    vouchers = sorted((v.number, v.total_amount, v.status) for v in db.scalars(select(Voucher)))
    stock = sorted(stock_balances(db, business_id).items())
    return vouchers, stock


def test_demo_scenario(client, db):
    shutil.rmtree(get_settings().backup_local_dir, ignore_errors=True)
    s = demo.SCENARIO
    d = today()

    # 1. 사업장·관리자 초기 설정 + 2단계 인증(간편 비밀번호) 등록
    res = _post(client, "/setup", {"biz_name": demo.BUSINESS["name"],
                "biz_no": demo.BUSINESS["biz_no"], "tax_type": "general", "name": "대표",
                "email": "owner@demo.kr", "password": PASSWORD})  # fmt: skip
    assert res.headers["location"] == "/account/pin"
    res = _post(client, "/account/pin", {"pin": PIN, "pin_confirm": PIN})
    assert res.status_code == 303

    # 2. 거래처·품목 엑셀 업로드
    for kind, data in (("partners", demo.partners_xlsx()), ("items", demo.items_xlsx())):
        res = _post(client, f"/master/import/{kind}", {}, "/master/import",
                    files={"file": (f"{kind}.xlsx", data)})  # fmt: skip
        assert res.status_code == 200, res.text
    assert len(db.scalars(select(Partner)).all()) == len(demo.PARTNERS)
    assert len(db.scalars(select(Item)).all()) == len(demo.ITEMS)
    business_id = db.scalar(select(Partner.business_id))

    units = {u.name: u.id for u in db.scalars(select(Unit))}
    partners = {p.name: p.id for p in db.scalars(select(Partner))}
    items = {i.name: i.id for i in db.scalars(select(Item))}

    def voucher(prefix, spec):
        return _post(client, prefix, {
            "voucher_date": d.isoformat(), "partner_id": partners[spec["partner"]],
            "item_id": items[spec["item"]], "unit_id": units[spec["unit"]],
            "qty": spec["qty"], "unit_price": spec["unit_price"], "warehouse_id": "",
            "line_memo": ""}, f"{prefix}/new")  # fmt: skip

    # 3. 매입: 생수 500mL 10박스 → 재고 200병 · 100L
    res = voucher("/purchase", s["purchase"])
    assert res.status_code == 303, res.text
    assert "200병 · 100L" in client.get("/inventory").text

    # 4. 매출 + 거래명세서 인쇄 → 재고 감소
    res = voucher("/sales", s["sale"])
    assert res.status_code == 303, res.text
    sale_path = res.headers["location"]
    printed = client.get(f"{sale_path}/print")
    assert printed.status_code == 200 and "66,000" in printed.text
    assert "100병 · 50L" in client.get("/inventory").text

    # 5. 파손 5병 폐기 등록·승인 → 가용 재고·손실 금액 반영
    wh = db.scalar(select(Warehouse).where(Warehouse.is_default))
    res = _post(client, "/inventory/disposals", {"disposal_date": d.isoformat(),
                "item_id": items[s["disposal"]["item"]], "warehouse_id": wh.id,
                "qty": s["disposal"]["qty"], "reason": "damaged", "memo": "파손"},
                "/inventory/disposals/new")  # fmt: skip
    assert res.status_code == 303, res.text
    disposal = db.scalar(select(Disposal))
    assert "5병" in client.get("/inventory").text  # 폐기 대기
    res = _post(client, f"/inventory/disposals/{disposal.id}/approve", {"pin": PIN},
                f"/inventory/disposals/{disposal.id}")  # fmt: skip
    assert res.status_code == 303
    db.expire_all()
    assert db.get(Disposal, disposal.id).loss_amount == Decimal(1500)  # 5병 × 300원
    assert "95병" in client.get("/inventory").text

    # 6. 홈택스 세금계산서 업로드 → 전표와 자동 매칭
    for direction in ("sales", "purchase"):
        upload = {"file": (f"{direction}.xlsx", demo.hometax_xlsx(direction, d))}
        res = _post(client, "/tax-invoice/upload", {}, "/tax-invoice/upload", files=upload)
        assert res.status_code == 200, res.text
    invoices = db.scalars(select(TaxInvoice)).all()
    assert len(invoices) == 2 and all(i.matched_at for i in invoices)

    # 7. 수금 → 미수금 감소, 대시보드에 이번 달 실적·폐기 손실
    res = _post(client, "/funds", {"kind": "receipt", "payment_date": d.isoformat(),
                "partner_id": partners[s["receipt"]["partner"]],
                "amount": s["receipt"]["amount"], "method": "transfer"},
                "/funds/new?kind=receipt")  # fmt: skip
    assert res.status_code == 303, res.text
    ledger = client.get("/receivables?only_open=1").text
    assert s["receipt"]["partner"] not in ledger
    dash = client.get("/").text
    assert "60,000" in dash and "1,500" in dash

    # 8. 백업 → 데이터가 바뀐 뒤 복원 → 전표·재고 수치 일치
    before = _numbers(db, business_id)
    res = _post(client, "/settings/backup", {}, "/settings/backup")
    assert res.status_code == 303
    name = backup.list_backups()[0].name
    res = voucher("/sales", {**s["sale"], "qty": 10})
    assert res.status_code == 303
    db.expire_all()
    assert _numbers(db, business_id) != before
    res = _post(client, "/settings/backup/restore", {"name": name, "password": PASSWORD},
                "/settings/backup")  # fmt: skip
    assert res.headers["location"] == "/login"
    db.expire_all()
    assert _numbers(db, business_id) == before


def test_demo_seed_builds_history(db):
    business = demo.seed(db, "owner@demo.kr", PASSWORD, months=2)
    sales = db.scalars(select(Voucher).where(Voucher.kind == "sales")).all()
    assert len(sales) > 20
    # 재고가 음수가 되지 않는다
    assert all(q >= 0 for q in stock_balances(db, business.id).values())
