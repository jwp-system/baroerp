"""전표 규칙: 거래 단위 → 재고 단위 환산, 금액·부가세 계산, 재고 이력 생성, 취소·역분개."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Business, Item, Partner, StockMove, User, Voucher, VoucherLine, Warehouse
from app.models.master import PartnerKind
from app.models.voucher import StockMoveKind, VoucherKind, VoucherStatus
from app.services import closing, numbering
from app.services.master import format_qty
from app.services.payments import voucher_allocated
from app.timeutil import today

VAT_RATE = Decimal("0.1")
WON = Decimal(1)
_PREFIX = {VoucherKind.SALES: "S", VoucherKind.PURCHASE: "P"}
_MOVE_KIND = {
    VoucherKind.SALES: StockMoveKind.SALES_OUT,
    VoucherKind.PURCHASE: StockMoveKind.PURCHASE_IN,
}
_SIGN = {VoucherKind.SALES: Decimal(-1), VoucherKind.PURCHASE: Decimal(1)}
_PARTNER_KINDS = {
    VoucherKind.SALES: {PartnerKind.SALES, PartnerKind.BOTH},
    VoucherKind.PURCHASE: {PartnerKind.PURCHASE, PartnerKind.BOTH},
}


class VoucherError(ValueError):
    pass


_INVOICE_LINKED = (
    "세금계산서와 대사된 전표라 거래처·금액을 바꾸거나 취소할 수 없어요. "
    "세금계산서 메뉴에서 대사를 먼저 풀어 주세요"
)


def _paid_label(kind: VoucherKind) -> str:
    return "수금" if kind == VoucherKind.SALES else "지급"


@dataclass
class LineInput:
    item_id: int
    unit_id: int
    qty: Decimal
    unit_price: Decimal
    warehouse_id: int | None = None
    memo: str = ""


# ---------- 재고 ----------


def stock_balances(
    db: Session, business_id: int, pairs: set[tuple[int, int]] | None = None, item_ids=None
) -> dict[tuple[int, int], Decimal]:
    """{(품목, 창고): 현재고(재고 단위)}"""
    stmt = (
        select(StockMove.item_id, StockMove.warehouse_id, func.sum(StockMove.qty))
        .where(StockMove.business_id == business_id)
        .group_by(StockMove.item_id, StockMove.warehouse_id)
    )
    ids = item_ids if item_ids is not None else ({i for i, _ in pairs} if pairs else None)
    if ids is not None:
        stmt = stmt.where(StockMove.item_id.in_(ids))
    rows = {(i, w): Decimal(q or 0) for i, w, q in db.execute(stmt)}
    if pairs is not None:
        return {p: rows.get(p, Decimal(0)) for p in pairs}
    return rows


def item_totals(db: Session, business_id: int, item_ids) -> dict[int, Decimal]:
    totals: dict[int, Decimal] = {}
    for (item_id, _), qty in stock_balances(db, business_id, item_ids=item_ids).items():
        totals[item_id] = totals.get(item_id, Decimal(0)) + qty
    return totals


def _check_negative(
    db: Session, business: Business, before: dict[tuple[int, int], Decimal]
) -> None:
    if business.allow_negative_stock or not before:
        return
    db.flush()
    after = stock_balances(db, business.id, set(before))
    short = [p for p, qty in after.items() if qty < 0 and qty < before[p]]
    if not short:
        return
    names = []
    for item_id, wh_id in short:
        item = db.get(Item, item_id)
        wh = db.get(Warehouse, wh_id)
        lack = -after[(item_id, wh_id)]
        names.append(f"{item.name}({wh.name}) {format_qty(lack, 4)}{item.stock_unit.symbol}")
    raise VoucherError("재고가 모자라요: " + ", ".join(names))


# ---------- 번호·금액 ----------


def next_number(db: Session, business_id: int, kind: VoucherKind, d: date) -> str:
    return numbering.next_number(db, Voucher, business_id, _PREFIX[kind], d)


def line_supply(qty: Decimal, unit_price: Decimal) -> Decimal:
    return (qty * unit_price).quantize(WON, ROUND_HALF_UP)


def compute_vat(business: Business, lines: list[VoucherLine]) -> Decimal:
    """부가세 = 과세 공급가 합계 × 10%, 원 단위 절사"""
    if not business.charges_vat:
        return Decimal(0)
    taxable = sum((ln.supply_amount for ln in lines if not ln.tax_exempt), Decimal(0))
    sign = -1 if taxable < 0 else 1
    return sign * (abs(taxable) * VAT_RATE).quantize(WON, ROUND_DOWN)


def _totals(business: Business, voucher: Voucher) -> None:
    voucher.supply_amount = sum((ln.supply_amount for ln in voucher.lines), Decimal(0))
    voucher.vat_amount = compute_vat(business, voucher.lines)
    voucher.total_amount = voucher.supply_amount + voucher.vat_amount


def _move_for(voucher: Voucher, line: VoucherLine, sign: Decimal) -> StockMove:
    unit_cost = None
    if voucher.kind == VoucherKind.PURCHASE and line.stock_qty:
        unit_cost = (line.supply_amount / line.stock_qty).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return StockMove(
        business_id=voucher.business_id,
        move_date=voucher.voucher_date,
        kind=_MOVE_KIND[VoucherKind(voucher.kind)],
        item_id=line.item_id,
        warehouse_id=line.warehouse_id,
        qty=sign * line.stock_qty,
        unit_cost=unit_cost,
        voucher_id=voucher.id,
        voucher_line_id=line.id,
    )


# ---------- 저장 ----------


def save_voucher(
    db: Session,
    business: Business,
    user: User,
    kind: VoucherKind,
    voucher: Voucher | None,
    voucher_date: date,
    partner_id: int,
    lines: list[LineInput],
    memo: str = "",
) -> Voucher:
    closing.ensure_open(db, business.id, voucher_date)
    linked = Decimal(0)
    if voucher is not None:
        if voucher.reversal_of_id or voucher.status != VoucherStatus.CONFIRMED:
            raise VoucherError("취소되었거나 취소 전표인 전표는 고칠 수 없어요")
        closing.ensure_open(db, business.id, voucher.voucher_date)
        linked = voucher_allocated(db, voucher.id)
        if linked and partner_id != voucher.partner_id:
            raise VoucherError(f"{_paid_label(kind)}된 전표라 거래처를 바꿀 수 없어요")
        old_amounts = (voucher.partner_id, voucher.supply_amount, voucher.vat_amount)

    partner = db.get(Partner, partner_id)
    if partner is None or partner.business_id != business.id:
        raise VoucherError("거래처를 골라 주세요")
    if partner.kind not in _PARTNER_KINDS[kind]:
        label = "매출" if kind == VoucherKind.SALES else "매입"
        raise VoucherError(f"'{partner.name}'은(는) {label} 거래처가 아니에요")
    if not lines:
        raise VoucherError("품목을 한 줄 이상 입력해 주세요")

    default_wh = db.scalar(
        select(Warehouse).where(Warehouse.business_id == business.id, Warehouse.is_default)
    )
    old_items = {ln.item_id for ln in voucher.lines} if voucher else set()
    built: list[VoucherLine] = []
    for no, inp in enumerate(lines, start=1):
        item = db.get(Item, inp.item_id)
        if item is None or item.business_id != business.id:
            raise VoucherError(f"{no}번째 줄: 품목을 골라 주세요")
        if not item.is_active and item.id not in old_items:
            raise VoucherError(f"{no}번째 줄: '{item.name}'은(는) 사용 안 함 품목이에요")
        if inp.unit_id == item.stock_unit_id:
            factor = Decimal(1)
        else:
            conv = next((u for u in item.units if u.unit_id == inp.unit_id), None)
            if conv is None:
                raise VoucherError(f"{no}번째 줄: '{item.name}'에 없는 거래 단위예요")
            factor = conv.factor
        if inp.qty <= 0:
            raise VoucherError(f"{no}번째 줄: 수량은 0보다 커야 해요")
        if inp.unit_price < 0:
            raise VoucherError(f"{no}번째 줄: 단가는 0 이상이어야 해요")
        stock_qty = inp.qty * factor
        decimals = item.stock_unit.decimals
        if stock_qty != stock_qty.quantize(Decimal(1).scaleb(-decimals), ROUND_DOWN):
            raise VoucherError(
                f"{no}번째 줄: '{item.name}' 재고 단위({item.stock_unit.name})는 "
                f"소수 {decimals}자리까지예요"
            )
        wh = db.get(Warehouse, inp.warehouse_id) if inp.warehouse_id else default_wh
        if wh is None or wh.business_id != business.id:
            raise VoucherError(f"{no}번째 줄: 창고를 골라 주세요")
        built.append(
            VoucherLine(
                business_id=business.id,
                line_no=no,
                item_id=item.id,
                warehouse_id=wh.id,
                unit_id=inp.unit_id,
                qty=inp.qty,
                unit_price=inp.unit_price,
                factor=factor,
                stock_qty=stock_qty,
                supply_amount=line_supply(inp.qty, inp.unit_price),
                tax_exempt=item.tax_exempt,
                memo=inp.memo.strip() or None,
            )
        )

    affected = {(ln.item_id, ln.warehouse_id) for ln in built}
    if voucher is not None:
        affected |= {(ln.item_id, ln.warehouse_id) for ln in voucher.lines}
    before = stock_balances(db, business.id, affected)

    if voucher is None:
        voucher = Voucher(
            business_id=business.id,
            kind=kind,
            number=next_number(db, business.id, kind, voucher_date),
            created_by_id=user.id,
        )
        db.add(voucher)
    else:
        db.execute(delete(StockMove).where(StockMove.voucher_id == voucher.id))
        voucher.lines.clear()
        db.flush()
    voucher.voucher_date = voucher_date
    voucher.partner_id = partner.id
    voucher.memo = memo.strip() or None
    voucher.lines.extend(built)
    _totals(business, voucher)
    if linked and voucher.total_amount < linked:
        raise VoucherError(
            f"이미 {_paid_label(kind)}된 {linked:,.0f}원보다 적게 고칠 수 없어요. "
            f"자금 메뉴에서 {_paid_label(kind)} 건을 먼저 취소해 주세요"
        )
    if voucher.tax_invoice_id and old_amounts != (
        voucher.partner_id, voucher.supply_amount, voucher.vat_amount
    ):  # fmt: skip
        raise VoucherError(_INVOICE_LINKED)
    db.flush()

    sign = _SIGN[kind]
    for line in voucher.lines:
        db.add(_move_for(voucher, line, sign))
    _check_negative(db, business, before)
    return voucher


# ---------- 취소 ----------


def cancel_voucher(db: Session, business: Business, user: User, voucher: Voucher) -> Voucher | None:
    """마감 전이면 전표를 취소하고 재고 이력을 지운다. 마감 후면 오늘 날짜로 취소 전표를 만든다.
    취소 전표를 만들었으면 그 전표를 돌려준다."""
    if voucher.reversal_of_id:
        raise VoucherError("취소 전표는 다시 취소할 수 없어요")
    if voucher.status != VoucherStatus.CONFIRMED:
        raise VoucherError("이미 취소된 전표예요")
    kind = VoucherKind(voucher.kind)
    if voucher_allocated(db, voucher.id):
        paid = _paid_label(kind)
        raise VoucherError(f"{paid}된 전표예요. 자금 메뉴에서 {paid} 건을 먼저 취소해 주세요")
    if voucher.tax_invoice_id:
        raise VoucherError(_INVOICE_LINKED)

    affected = {(ln.item_id, ln.warehouse_id) for ln in voucher.lines}
    before = stock_balances(db, business.id, affected)

    if not closing.is_closed(db, business.id, voucher.voucher_date):
        db.execute(delete(StockMove).where(StockMove.voucher_id == voucher.id))
        voucher.status = VoucherStatus.CANCELED
        _check_negative(db, business, before)
        return None

    reversal_date = today()
    closing.ensure_open(db, business.id, reversal_date)
    reversal = Voucher(
        business_id=business.id,
        kind=kind,
        number=next_number(db, business.id, kind, reversal_date),
        voucher_date=reversal_date,
        partner_id=voucher.partner_id,
        memo=f"{voucher.number} 취소",
        reversal_of_id=voucher.id,
        created_by_id=user.id,
    )
    for ln in voucher.lines:
        reversal.lines.append(
            VoucherLine(
                business_id=business.id,
                line_no=ln.line_no,
                item_id=ln.item_id,
                warehouse_id=ln.warehouse_id,
                unit_id=ln.unit_id,
                qty=-ln.qty,
                unit_price=ln.unit_price,
                factor=ln.factor,
                stock_qty=-ln.stock_qty,
                supply_amount=-ln.supply_amount,
                tax_exempt=ln.tax_exempt,
                memo=ln.memo,
            )
        )
    _totals(business, reversal)
    db.add(reversal)
    db.flush()
    sign = _SIGN[kind]
    for line in reversal.lines:
        db.add(_move_for(reversal, line, sign))
    voucher.status = VoucherStatus.REVERSED
    _check_negative(db, business, before)
    return reversal
