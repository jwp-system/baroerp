"""기준정보 엑셀 일괄 등록. 한 행이라도 오류가 있으면 아무것도 저장하지 않는다."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import BinaryIO

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, Item, ItemGroup, ItemUnit, Partner, Unit
from app.models.master import PARTNER_KIND_LABELS
from app.services import master as svc
from app.services.nts import biz_no_error, digits

MAX_ROWS = 5000

# (머리글, 필수 여부, 예시)
PARTNER_COLUMNS = [
    ("거래처명", True, "바로유통"),
    ("구분", True, "매출"),
    ("사업자번호", False, "123-45-67890"),
    ("대표자", False, "홍길동"),
    ("업태", False, "도소매"),
    ("종목", False, "식품"),
    ("주소", False, "서울시 중구 세종대로 1"),
    ("전화", False, "02-123-4567"),
    ("이메일", False, "tax@example.com"),
    ("담당자", False, "김담당"),
    ("담당자 연락처", False, "010-1234-5678"),
    ("담당자 이메일", False, "kim@example.com"),
    ("결제조건(일)", False, 30),
    ("여신한도", False, 10000000),
    ("메모", False, ""),
]

ITEM_COLUMNS = [
    ("식별코드", False, ""),
    ("품목명", True, "생수 500mL"),
    ("품목그룹", True, "상품 > 음료 > 생수"),
    ("규격", False, "500mL"),
    ("재고단위", True, "병"),
    ("단위용량", False, 500),
    ("용량단위", False, "mL"),
    ("거래단위", False, "박스"),
    ("환산수량", False, 20),
    ("매입기본단위", False, "박스"),
    ("매출기본단위", False, "병"),
    ("매입단가", False, 300),
    ("판매단가", False, 600),
    ("과세구분", False, "과세"),
    ("적정재고", False, 100),
]


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    partner_ids: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class RowError(Exception):
    pass


def _read_rows(file: BinaryIO, columns) -> list[tuple[int, dict]]:
    try:
        wb = load_workbook(file, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl은 다양한 예외를 던진다
        raise RowError("엑셀(.xlsx) 파일을 열 수 없어요") from exc
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows, ())]
    missing = [name for name, required, _ in columns if required and name not in header]
    if missing:
        raise RowError(f"필수 열이 없어요: {', '.join(missing)}. 양식을 내려받아 써 주세요")

    out = []
    for idx, values in enumerate(rows, start=2):
        if values is None or all(v is None or str(v).strip() == "" for v in values):
            continue
        row = {
            header[i]: (v.strip() if isinstance(v, str) else v)
            for i, v in enumerate(values)
            if i < len(header) and header[i]
        }
        out.append((idx, row))
        if len(out) > MAX_ROWS:
            raise RowError(f"한 번에 {MAX_ROWS:,}행까지 올릴 수 있어요")
    wb.close()
    return out


def _text(row, key) -> str | None:
    v = row.get(key)
    if v is None:
        return None
    text = str(v).strip()
    return text or None


def _number(row, key, label=None) -> Decimal | None:
    v = row.get(key)
    if v is None or str(v).strip() == "":
        return None
    try:
        return Decimal(str(v).replace(",", "").strip())
    except InvalidOperation as exc:
        raise RowError(f"{label or key}은(는) 숫자여야 해요") from exc


def _non_stock(unit: Unit | None, stock_unit: Unit) -> int | None:
    """기본 거래 단위: 재고 단위와 같으면 비워 둔다"""
    return unit.id if unit is not None and unit.id != stock_unit.id else None


_KIND_BY_LABEL = {label: kind for kind, label in PARTNER_KIND_LABELS.items()}


# ---------- 거래처 ----------


def import_partners(db: Session, business: Business, file: BinaryIO) -> ImportResult:
    result = ImportResult()
    try:
        rows = _read_rows(file, PARTNER_COLUMNS)
    except RowError as exc:
        result.errors.append((0, str(exc)))
        return result

    by_biz_no = {
        p.biz_no: p
        for p in db.scalars(select(Partner).where(Partner.business_id == business.id))
        if p.biz_no
    }
    by_name_no_biz = {
        p.name: p
        for p in db.scalars(
            select(Partner).where(Partner.business_id == business.id, Partner.biz_no.is_(None))
        )
    }
    touched: list[Partner] = []

    for line, row in rows:
        try:
            name = _text(row, "거래처명")
            if not name:
                raise RowError("거래처명을 입력해 주세요")
            kind = _KIND_BY_LABEL.get(_text(row, "구분") or "")
            if kind is None:
                raise RowError("구분은 매출·매입·공통 중 하나예요")
            biz_no = digits(_text(row, "사업자번호")) or None
            if biz_no and (msg := biz_no_error(biz_no)):
                raise RowError(msg)
            terms = _number(row, "결제조건(일)")
            credit = _number(row, "여신한도")
        except RowError as exc:
            result.errors.append((line, str(exc)))
            continue

        partner = by_biz_no.get(biz_no) if biz_no else by_name_no_biz.get(name)
        if partner is None:
            partner = Partner(business_id=business.id, name=name, kind=kind, biz_no=biz_no)
            db.add(partner)
            result.created += 1
            if biz_no:
                by_biz_no[biz_no] = partner
            else:
                by_name_no_biz[name] = partner
        else:
            result.updated += 1
        partner.name = name
        partner.kind = kind
        partner.ceo_name = _text(row, "대표자")
        partner.biz_type = _text(row, "업태")
        partner.biz_item = _text(row, "종목")
        partner.address = _text(row, "주소")
        partner.phone = _text(row, "전화")
        partner.email = _text(row, "이메일")
        partner.contact_name = _text(row, "담당자")
        partner.contact_phone = _text(row, "담당자 연락처")
        partner.contact_email = _text(row, "담당자 이메일")
        if terms is not None:
            partner.payment_terms_days = int(terms)
        partner.credit_limit = credit
        partner.memo = _text(row, "메모")
        touched.append(partner)

    if result.errors:
        db.rollback()
        result.created = result.updated = 0
        return result
    db.commit()
    result.partner_ids = [p.id for p in touched if p.biz_no]
    return result


# ---------- 품목 ----------


class _GroupResolver:
    """'식별코드' 또는 '대분류 > 중분류 > 소분류' 경로로 그룹을 찾고, 없는 경로는 만든다."""

    def __init__(self, db: Session, business: Business):
        self.db = db
        self.business = business
        self.groups = svc.groups_of(db, business.id)
        self.by_code = {g.code_key: g for g in self.groups}

    def _child(self, parent: ItemGroup | None, name: str) -> ItemGroup | None:
        pid = parent.id if parent else None
        return next((g for g in self.groups if g.parent_id == pid and g.name == name), None)

    def _new_code(self) -> str:
        n = 1
        while f"G{n:04d}" in self.by_code:
            n += 1
        return f"G{n:04d}"

    def resolve(self, value: str) -> ItemGroup:
        if svc.code_key(value) in self.by_code:
            return self.by_code[svc.code_key(value)]
        names = [n.strip() for n in value.split(">") if n.strip()]
        parent: ItemGroup | None = None
        for name in names:
            node = self._child(parent, name)
            if node is None:
                try:
                    level = svc.validate_parent(self.db, self.business, None, parent)
                except svc.MasterError as exc:
                    raise RowError(str(exc)) from exc
                code = self._new_code()
                node = ItemGroup(
                    business_id=self.business.id,
                    code=code,
                    code_key=code,
                    name=name,
                    parent=parent,
                    parent_id=parent.id if parent else None,
                    level=level,
                )
                self.db.add(node)
                self.db.flush()
                self.groups.append(node)
                self.by_code[code] = node
            parent = node
        if parent is None:
            raise RowError("품목그룹을 입력해 주세요")
        if any(g.parent_id == parent.id for g in self.groups):
            raise RowError(f"'{parent.name}'에는 하위 그룹이 있어요. 가장 아래 그룹을 적어 주세요")
        return parent


def import_items(db: Session, business: Business, file: BinaryIO) -> ImportResult:
    result = ImportResult()
    try:
        rows = _read_rows(file, ITEM_COLUMNS)
    except RowError as exc:
        result.errors.append((0, str(exc)))
        return result

    units = list(db.scalars(select(Unit).where(Unit.business_id == business.id)))
    unit_by = {k: u for u in units for k in (u.name, u.symbol, u.name.lower(), u.symbol.lower())}
    items = {i.code_key: i for i in db.scalars(select(Item).where(Item.business_id == business.id))}
    groups = _GroupResolver(db, business)

    def unit_of(row, key, required=False) -> Unit | None:
        name = _text(row, key)
        if not name:
            if required:
                raise RowError(f"{key}를 입력해 주세요")
            return None
        unit = unit_by.get(name) or unit_by.get(name.lower())
        if unit is None:
            raise RowError(f"'{name}' 단위가 없어요. 기준정보 > 단위에서 먼저 추가해 주세요")
        return unit

    for line, row in rows:
        try:
            name = _text(row, "품목명")
            if not name:
                raise RowError("품목명을 입력해 주세요")
            group = groups.resolve(_text(row, "품목그룹") or "")
            stock_unit = unit_of(row, "재고단위", required=True)
            content_qty = _number(row, "단위용량")
            content_unit = unit_of(row, "용량단위")
            if (content_qty is None) != (content_unit is None):
                raise RowError("단위용량과 용량단위는 함께 입력해 주세요")
            trade_unit = unit_of(row, "거래단위")
            factor = _number(row, "환산수량")
            if trade_unit is not None and (factor is None or factor <= 0):
                raise RowError("거래단위를 쓰면 환산수량(재고단위 기준)을 입력해 주세요")
            tax_text = _text(row, "과세구분") or "과세"
            if tax_text not in ("과세", "면세"):
                raise RowError("과세구분은 과세 또는 면세예요")
            purchase_price = _number(row, "매입단가") or Decimal(0)
            sale_price = _number(row, "판매단가") or Decimal(0)
            safety = _number(row, "적정재고")
            purchase_unit = unit_of(row, "매입기본단위")
            sale_unit = unit_of(row, "매출기본단위")

            code = _text(row, "식별코드")
            item = items.get(svc.code_key(code)) if code else None
            if item is None:
                code = code or svc.next_item_code(db, business, group)
                item = Item(business_id=business.id, code=code.strip(), code_key=svc.code_key(code))
                db.add(item)
                items[item.code_key] = item
                result.created += 1
            else:
                result.updated += 1
            allowed = {stock_unit.id, trade_unit.id if trade_unit else None}
            allowed |= {u.unit_id for u in item.units}
            for unit, label in ((purchase_unit, "매입기본단위"), (sale_unit, "매출기본단위")):
                if unit is not None and unit.id not in allowed:
                    raise RowError(f"{label}는 재고단위나 거래단위 중 하나여야 해요")
        except RowError as exc:
            result.errors.append((line, str(exc)))
            continue

        item.name = name
        item.group_id = group.id
        item.spec = _text(row, "규격")
        item.stock_unit_id = stock_unit.id
        item.content_qty = content_qty
        item.content_unit_id = content_unit.id if content_unit else None
        item.purchase_price = purchase_price
        item.sale_price = sale_price
        item.tax_exempt = tax_text == "면세"
        item.safety_stock = safety
        item.purchase_unit_id = _non_stock(purchase_unit, stock_unit)
        item.sale_unit_id = _non_stock(sale_unit, stock_unit)
        if trade_unit is not None and trade_unit.id != stock_unit.id:
            existing = next((u for u in item.units if u.unit_id == trade_unit.id), None)
            if existing:
                existing.factor = factor
            else:
                item.units.append(
                    ItemUnit(business_id=business.id, unit_id=trade_unit.id, factor=factor)
                )
        # 같은 파일 안에서 다음 행 채번이 이어지도록 반영
        db.flush()

    if result.errors:
        db.rollback()
        result.created = result.updated = 0
        return result
    db.commit()
    return result
