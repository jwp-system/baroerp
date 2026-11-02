"""재고 현황, 이동평균 단가, 폐기(등록 → 승인 → 손실 분개), 실사 재고 조정."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    Business,
    Disposal,
    Item,
    JournalEntry,
    StockAdjustment,
    StockAdjustmentLine,
    StockMove,
    StockTransfer,
    StockTransferLine,
    User,
    Warehouse,
)
from app.models.accounting import JournalSource
from app.models.inventory import AdjustmentStatus, DisposalReason, DisposalStatus
from app.models.voucher import StockMoveKind
from app.services import accounting, closing, numbering
from app.services.master import format_qty
from app.services.voucher import stock_balances
from app.timeutil import today

WON = Decimal(1)
CENT = Decimal("0.01")


class InventoryError(ValueError):
    pass


# ---------- 조회 ----------


def pending_disposals(db: Session, business_id: int) -> dict[tuple[int, int], Decimal]:
    """{(품목, 창고): 폐기 대기 수량}"""
    rows = db.execute(
        select(Disposal.item_id, Disposal.warehouse_id, func.sum(Disposal.qty))
        .where(Disposal.business_id == business_id, Disposal.status == DisposalStatus.PENDING)
        .group_by(Disposal.item_id, Disposal.warehouse_id)
    )
    return {(i, w): Decimal(q or 0) for i, w, q in rows}


def disposed_between(
    db: Session, business_id: int, start: date, end: date
) -> dict[tuple[int, int], Decimal]:
    """기간 폐기 누계(승인분, 취소 전표는 음수로 상계)"""
    rows = db.execute(
        select(StockMove.item_id, StockMove.warehouse_id, func.sum(StockMove.qty))
        .where(
            StockMove.business_id == business_id,
            StockMove.kind == StockMoveKind.DISPOSAL,
            StockMove.move_date >= start,
            StockMove.move_date <= end,
        )
        .group_by(StockMove.item_id, StockMove.warehouse_id)
    )
    return {(i, w): -Decimal(q or 0) for i, w, q in rows}


def stock_as_of(db: Session, business_id: int, warehouse_id: int, as_of: date) -> dict:
    """{품목: as_of 일자 기준 장부 수량}"""
    rows = db.execute(
        select(StockMove.item_id, func.sum(StockMove.qty))
        .where(
            StockMove.business_id == business_id,
            StockMove.warehouse_id == warehouse_id,
            StockMove.move_date <= as_of,
        )
        .group_by(StockMove.item_id)
    )
    return {i: Decimal(q or 0) for i, q in rows}


def moving_average_cost(db: Session, item: Item, as_of: date) -> Decimal:
    """as_of 일자까지의 이동평균 단가(재고 단위 기준, 전 창고 합산).
    입고 원가가 없으면 품목의 매입단가를 쓴다."""
    moves = db.execute(
        select(StockMove.qty, StockMove.unit_cost)
        .where(
            StockMove.business_id == item.business_id,
            StockMove.item_id == item.id,
            StockMove.move_date <= as_of,
        )
        .order_by(StockMove.move_date, StockMove.id)
    )
    qty, avg = Decimal(0), None
    for q, cost in moves:
        q = Decimal(q)
        if cost is not None and q > 0:
            cost = Decimal(cost)
            avg = cost if avg is None or qty <= 0 else (qty * avg + q * cost) / (qty + q)
        qty += q
    if avg is None:
        return Decimal(item.purchase_price or 0)
    return avg.quantize(CENT, ROUND_HALF_UP)


@dataclass
class StockRow:
    item: Item
    warehouse_id: int | None
    qty: Decimal
    pending: Decimal
    disposed: Decimal

    @property
    def available(self) -> Decimal:
        return self.qty - self.pending


def stock_rows(
    db: Session,
    business_id: int,
    items: list[Item],
    warehouse_id: int | None,
    start: date,
    end: date,
) -> list[StockRow]:
    """재고 현황: 현재고, 폐기 대기, 가용, 기간 폐기 누계. 창고를 고르지 않으면 합산."""
    ids = [i.id for i in items]
    balances = stock_balances(db, business_id, item_ids=ids)
    pending = pending_disposals(db, business_id)
    disposed = disposed_between(db, business_id, start, end)

    def total(src: dict, item_id: int) -> Decimal:
        return sum(
            (q for (i, w), q in src.items() if i == item_id and warehouse_id in (None, w)),
            Decimal(0),
        )

    return [
        StockRow(
            item=item,
            warehouse_id=warehouse_id,
            qty=total(balances, item.id),
            pending=total(pending, item.id),
            disposed=total(disposed, item.id),
        )
        for item in items
    ]


def _check_decimals(item: Item, qty: Decimal) -> None:
    decimals = item.stock_unit.decimals
    if qty != qty.quantize(Decimal(1).scaleb(-decimals), ROUND_DOWN):
        raise InventoryError(
            f"'{item.name}' 재고 단위({item.stock_unit.name})는 소수 {decimals}자리까지예요"
        )


def _owned(db: Session, model, obj_id: int | None, business_id: int):
    obj = db.get(model, obj_id) if obj_id else None
    return obj if obj is not None and obj.business_id == business_id else None


# ---------- 폐기 ----------


def create_disposal(
    db: Session,
    business: Business,
    user: User,
    disposal_date: date,
    item_id: int,
    warehouse_id: int,
    qty: Decimal,
    reason: DisposalReason,
    memo: str = "",
) -> Disposal:
    """폐기 등록 → '승인 대기'(격리). 재고는 승인할 때 빠진다."""
    closing.ensure_open(db, business.id, disposal_date)
    item = _owned(db, Item, item_id, business.id)
    if item is None:
        raise InventoryError("품목을 골라 주세요")
    wh = _owned(db, Warehouse, warehouse_id, business.id)
    if wh is None:
        raise InventoryError("창고를 골라 주세요")
    if qty <= 0:
        raise InventoryError("폐기 수량은 0보다 커야 해요")
    _check_decimals(item, qty)
    if not business.allow_negative_stock:
        current = stock_balances(db, business.id, {(item.id, wh.id)})[(item.id, wh.id)]
        available = current - pending_disposals(db, business.id).get((item.id, wh.id), Decimal(0))
        if qty > available:
            unit = item.stock_unit.name
            raise InventoryError(
                f"가용 재고보다 많아요: {wh.name}의 '{item.name}' 가용 재고는 "
                f"{format_qty(available, 4)}{unit}이에요"
            )
    disposal = Disposal(
        business_id=business.id,
        number=numbering.next_number(db, Disposal, business.id, "D", disposal_date),
        disposal_date=disposal_date,
        item_id=item.id,
        warehouse_id=wh.id,
        qty=qty,
        reason=reason,
        memo=memo.strip() or None,
        created_by_id=user.id,
    )
    db.add(disposal)
    db.flush()
    return disposal


def _disposal_move(d: Disposal) -> StockMove:
    return StockMove(
        business_id=d.business_id,
        move_date=d.disposal_date,
        kind=StockMoveKind.DISPOSAL,
        item_id=d.item_id,
        warehouse_id=d.warehouse_id,
        qty=-d.qty,
        unit_cost=d.unit_cost,
        disposal_id=d.id,
    )


def approve_disposal(db: Session, business: Business, user: User, d: Disposal) -> None:
    """승인: 이동평균 단가로 손실 금액을 정하고 재고 차감 + (차) 폐기손실 / (대) 상품"""
    if d.status != DisposalStatus.PENDING:
        raise InventoryError("승인 대기 중인 폐기만 승인할 수 있어요")
    closing.ensure_open(db, business.id, d.disposal_date)
    if not business.allow_negative_stock:
        current = stock_balances(db, business.id, {(d.item_id, d.warehouse_id)})
        if current[(d.item_id, d.warehouse_id)] < d.qty:
            raise InventoryError("재고가 모자라 승인할 수 없어요. 재고 현황을 확인해 주세요")
    d.unit_cost = moving_average_cost(db, d.item, d.disposal_date)
    d.loss_amount = (d.qty * d.unit_cost).quantize(WON, ROUND_HALF_UP)
    d.status = DisposalStatus.APPROVED
    d.approved_by_id = user.id
    d.approved_at = datetime.now(UTC)
    db.add(_disposal_move(d))
    entry = accounting.post(
        db, business.id, d.disposal_date, JournalSource.DISPOSAL,
        f"{d.number} {d.item.name} {d.reason_label} 폐기",
        accounting.loss_lines(
            accounting.disposal_loss_account(db, business),
            accounting.account(db, business.id, "inventory"),
            d.loss_amount,
        ),
    )  # fmt: skip
    d.journal_entry_id = entry.id if entry else None


def _drop_journal(db: Session, entry_id: int | None) -> None:
    if entry_id:
        entry = db.get(JournalEntry, entry_id)
        if entry is not None:
            db.delete(entry)


def unapprove_disposal(db: Session, business: Business, d: Disposal) -> None:
    """마감 전 승인 취소: 재고·분개를 되돌리고 다시 승인 대기로"""
    if d.status != DisposalStatus.APPROVED or d.reversal_of_id:
        raise InventoryError("승인된 폐기만 승인을 취소할 수 있어요")
    closing.ensure_open(db, business.id, d.disposal_date)
    db.execute(delete(StockMove).where(StockMove.disposal_id == d.id))
    entry_id, d.journal_entry_id = d.journal_entry_id, None
    db.flush()
    _drop_journal(db, entry_id)
    d.status = DisposalStatus.PENDING
    d.unit_cost = None
    d.loss_amount = Decimal(0)
    d.approved_by_id = None
    d.approved_at = None


def cancel_disposal(db: Session, business: Business, user: User, d: Disposal) -> Disposal | None:
    """승인 대기는 취소, 마감된 달의 승인 건은 오늘 날짜 취소 전표(재고 복원 + 역분개)"""
    if d.reversal_of_id:
        raise InventoryError("취소 전표는 다시 취소할 수 없어요")
    if d.status == DisposalStatus.PENDING:
        d.status = DisposalStatus.CANCELED
        return None
    if d.status != DisposalStatus.APPROVED:
        raise InventoryError("이미 취소된 폐기예요")
    if not closing.is_closed(db, business.id, d.disposal_date):
        unapprove_disposal(db, business, d)
        d.status = DisposalStatus.CANCELED
        return None

    rev_date = today()
    closing.ensure_open(db, business.id, rev_date)
    rev = Disposal(
        business_id=business.id,
        number=numbering.next_number(db, Disposal, business.id, "D", rev_date),
        disposal_date=rev_date,
        item_id=d.item_id,
        warehouse_id=d.warehouse_id,
        qty=-d.qty,
        reason=d.reason,
        memo=f"{d.number} 취소",
        status=DisposalStatus.APPROVED,
        unit_cost=d.unit_cost,
        loss_amount=-d.loss_amount,
        created_by_id=user.id,
        approved_by_id=user.id,
        approved_at=datetime.now(UTC),
        reversal_of_id=d.id,
    )
    db.add(rev)
    db.flush()
    db.add(_disposal_move(rev))
    entry = accounting.reverse(db, d.journal_entry, rev_date, f"{d.number} 폐기 취소")
    rev.journal_entry_id = entry.id if entry else None
    d.status = DisposalStatus.REVERSED
    return rev


# ---------- 실사 재고 조정 ----------


def save_adjustment(
    db: Session,
    business: Business,
    user: User,
    count_date: date,
    warehouse_id: int,
    counts: dict[int, Decimal],
    memo: str = "",
) -> StockAdjustment:
    """실사 수량을 받아 장부와의 차이만큼 재고를 맞춘다. 감모(부족)는 손실, 초과는 손실 차감."""
    closing.ensure_open(db, business.id, count_date)
    if count_date > today():
        raise InventoryError("실사일은 오늘 이후일 수 없어요")
    wh = _owned(db, Warehouse, warehouse_id, business.id)
    if wh is None:
        raise InventoryError("창고를 골라 주세요")
    if not counts:
        raise InventoryError("실사 수량을 한 품목 이상 입력해 주세요")
    book = stock_as_of(db, business.id, wh.id, count_date)
    adj = StockAdjustment(
        business_id=business.id,
        number=numbering.next_number(db, StockAdjustment, business.id, "A", count_date),
        count_date=count_date,
        warehouse_id=wh.id,
        memo=memo.strip() or None,
        created_by_id=user.id,
    )
    for item_id, counted in counts.items():
        item = _owned(db, Item, item_id, business.id)
        if item is None:
            raise InventoryError("없는 품목이 있어요")
        if counted < 0:
            raise InventoryError(f"'{item.name}' 실사 수량은 0 이상이어야 해요")
        _check_decimals(item, counted)
        book_qty = book.get(item.id, Decimal(0))
        diff = counted - book_qty
        cost = moving_average_cost(db, item, count_date)
        adj.lines.append(
            StockAdjustmentLine(
                business_id=business.id,
                item_id=item.id,
                book_qty=book_qty,
                counted_qty=counted,
                diff_qty=diff,
                unit_cost=cost,
                amount=(diff * cost).quantize(WON, ROUND_HALF_UP),
            )
        )
    adj.amount = sum((ln.amount for ln in adj.lines), Decimal(0))
    db.add(adj)
    db.flush()
    _post_adjustment(db, business, adj)
    return adj


def _post_adjustment(db: Session, business: Business, adj: StockAdjustment) -> None:
    for ln in adj.lines:
        if ln.diff_qty:
            db.add(
                StockMove(
                    business_id=business.id,
                    move_date=adj.count_date,
                    kind=StockMoveKind.ADJUST,
                    item_id=ln.item_id,
                    warehouse_id=adj.warehouse_id,
                    qty=ln.diff_qty,
                    unit_cost=ln.unit_cost,
                    adjustment_id=adj.id,
                )
            )
    entry = accounting.post(
        db, business.id, adj.count_date, JournalSource.ADJUSTMENT,
        f"{adj.number} 실사 재고 조정",
        accounting.loss_lines(
            accounting.account(db, business.id, "shrinkage_loss"),
            accounting.account(db, business.id, "inventory"),
            -adj.amount,  # 감모(음수 금액) → 손실(양수)
        ),
    )  # fmt: skip
    adj.journal_entry_id = entry.id if entry else None


def cancel_adjustment(
    db: Session, business: Business, user: User, adj: StockAdjustment
) -> StockAdjustment | None:
    if adj.reversal_of_id:
        raise InventoryError("취소 전표는 다시 취소할 수 없어요")
    if adj.status != AdjustmentStatus.CONFIRMED:
        raise InventoryError("이미 취소된 재고 조정이에요")
    if not closing.is_closed(db, business.id, adj.count_date):
        db.execute(delete(StockMove).where(StockMove.adjustment_id == adj.id))
        entry_id, adj.journal_entry_id = adj.journal_entry_id, None
        db.flush()
        _drop_journal(db, entry_id)
        adj.status = AdjustmentStatus.CANCELED
        return None

    rev_date = today()
    closing.ensure_open(db, business.id, rev_date)
    rev = StockAdjustment(
        business_id=business.id,
        number=numbering.next_number(db, StockAdjustment, business.id, "A", rev_date),
        count_date=rev_date,
        warehouse_id=adj.warehouse_id,
        amount=-adj.amount,
        memo=f"{adj.number} 취소",
        created_by_id=user.id,
        reversal_of_id=adj.id,
    )
    for ln in adj.lines:
        rev.lines.append(
            StockAdjustmentLine(
                business_id=business.id,
                item_id=ln.item_id,
                book_qty=ln.counted_qty,
                counted_qty=ln.book_qty,
                diff_qty=-ln.diff_qty,
                unit_cost=ln.unit_cost,
                amount=-ln.amount,
            )
        )
    db.add(rev)
    db.flush()
    _post_adjustment(db, business, rev)
    adj.status = AdjustmentStatus.REVERSED
    return rev


# ---------- 창고 이동 ----------


def _ensure_not_negative(db: Session, business: Business, before: dict) -> None:
    """이동·취소로 재고가 음수가 되면 막는다(이미 음수였던 곳이 더 줄지 않는 한 허용)"""
    if business.allow_negative_stock:
        return
    db.flush()
    after = stock_balances(db, business.id, set(before))
    short = [p for p, q in after.items() if q < 0 and q < before[p]]
    if short:
        item_id, wh_id = short[0]
        item, wh = db.get(Item, item_id), db.get(Warehouse, wh_id)
        lack = format_qty(-after[short[0]], 4)
        raise InventoryError(
            f"재고가 모자라요: {wh.name}의 '{item.name}' {lack}{item.stock_unit.name}"
        )


def _transfer_moves(db: Session, t: StockTransfer) -> None:
    for ln in t.lines:
        for wh_id, sign in ((t.from_warehouse_id, -1), (t.to_warehouse_id, 1)):
            db.add(
                StockMove(
                    business_id=t.business_id,
                    move_date=t.transfer_date,
                    kind=StockMoveKind.TRANSFER,
                    item_id=ln.item_id,
                    warehouse_id=wh_id,
                    qty=sign * ln.qty,
                    transfer_id=t.id,
                )
            )


def _transfer_pairs(t: StockTransfer) -> set[tuple[int, int]]:
    return {(ln.item_id, w) for ln in t.lines for w in (t.from_warehouse_id, t.to_warehouse_id)}


def save_transfer(
    db: Session,
    business: Business,
    user: User,
    transfer_date: date,
    from_warehouse_id: int,
    to_warehouse_id: int,
    quantities: dict[int, Decimal],
    memo: str = "",
) -> StockTransfer:
    """창고 이동: 품목마다 보내는 창고 −수량, 받는 창고 +수량. 원가·분개는 없다."""
    closing.ensure_open(db, business.id, transfer_date)
    src = _owned(db, Warehouse, from_warehouse_id, business.id)
    dst = _owned(db, Warehouse, to_warehouse_id, business.id)
    if src is None or dst is None:
        raise InventoryError("보내는 창고와 받는 창고를 골라 주세요")
    if src.id == dst.id:
        raise InventoryError("보내는 창고와 받는 창고가 같아요")
    if not dst.is_active:
        raise InventoryError(f"'{dst.name}'은(는) 사용 안 함 창고예요")
    lines = {i: q for i, q in quantities.items() if q}
    if not lines:
        raise InventoryError("이동할 수량을 한 품목 이상 입력해 주세요")

    pending = pending_disposals(db, business.id)
    current = stock_balances(db, business.id, {(i, src.id) for i in lines})
    t = StockTransfer(
        business_id=business.id,
        number=numbering.next_number(db, StockTransfer, business.id, "T", transfer_date),
        transfer_date=transfer_date,
        from_warehouse_id=src.id,
        to_warehouse_id=dst.id,
        memo=memo.strip() or None,
        created_by_id=user.id,
    )
    for item_id, qty in lines.items():
        item = _owned(db, Item, item_id, business.id)
        if item is None:
            raise InventoryError("없는 품목이 있어요")
        if qty < 0:
            raise InventoryError(f"'{item.name}' 이동 수량은 0보다 커야 해요")
        _check_decimals(item, qty)
        available = current[(item.id, src.id)] - pending.get((item.id, src.id), Decimal(0))
        if qty > available and not business.allow_negative_stock:
            raise InventoryError(
                f"'{item.name}'은(는) {src.name}에 가용 재고가 "
                f"{format_qty(available, 4)}{item.stock_unit.name}뿐이에요"
            )
        t.lines.append(StockTransferLine(business_id=business.id, item_id=item.id, qty=qty))
    db.add(t)
    db.flush()
    _transfer_moves(db, t)
    db.flush()
    return t


def cancel_transfer(
    db: Session, business: Business, user: User, t: StockTransfer
) -> StockTransfer | None:
    """마감 전이면 이동을 지우고, 마감된 달이면 오늘 날짜로 반대 방향 이동(취소 전표)"""
    if t.reversal_of_id:
        raise InventoryError("취소 전표는 다시 취소할 수 없어요")
    if t.status != AdjustmentStatus.CONFIRMED:
        raise InventoryError("이미 취소된 창고 이동이에요")
    before = stock_balances(db, business.id, _transfer_pairs(t))
    if not closing.is_closed(db, business.id, t.transfer_date):
        db.execute(delete(StockMove).where(StockMove.transfer_id == t.id))
        t.status = AdjustmentStatus.CANCELED
        _ensure_not_negative(db, business, before)
        return None

    rev_date = today()
    closing.ensure_open(db, business.id, rev_date)
    rev = StockTransfer(
        business_id=business.id,
        number=numbering.next_number(db, StockTransfer, business.id, "T", rev_date),
        transfer_date=rev_date,
        from_warehouse_id=t.to_warehouse_id,
        to_warehouse_id=t.from_warehouse_id,
        memo=f"{t.number} 취소",
        created_by_id=user.id,
        reversal_of_id=t.id,
    )
    for ln in t.lines:
        rev.lines.append(StockTransferLine(business_id=business.id, item_id=ln.item_id, qty=ln.qty))
    db.add(rev)
    db.flush()
    _transfer_moves(db, rev)
    t.status = AdjustmentStatus.REVERSED
    _ensure_not_negative(db, business, before)
    return rev
