"""재고 현황·폐기·실사 조정. 기획안 데모 시나리오 5번(파손 5병 폐기 등록·승인)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Disposal, JournalEntry, StockMove, Warehouse
from app.models.inventory import DisposalReason, DisposalStatus
from app.models.voucher import StockMoveKind
from app.services import closing
from app.services import inventory as inv
from app.timeutil import today
from tests.test_vouchers import _buy, _sell, _stock


def _wh(db):
    return db.scalar(select(Warehouse).where(Warehouse.is_default))


def _dispose(db, business, s, qty=5, d=None):
    disposal = inv.create_disposal(
        db, business, s["user"], d or today(), s["water"].id, _wh(db).id, Decimal(qty),
        DisposalReason.DAMAGED, "박스 파손",
    )  # fmt: skip
    db.commit()
    return disposal


def _journal(db, entry_id):
    entry = db.get(JournalEntry, entry_id)
    return [(ln.account.name, ln.debit, ln.credit) for ln in entry.lines]


def test_moving_average_cost(db, business, setup):
    _buy(db, business, setup, boxes=10, price=6000)  # 200병 × 300원
    _sell(db, business, setup, 100)
    _buy(db, business, setup, boxes=5, price=8000)  # 100병 × 400원
    # (100 × 300 + 100 × 400) / 200 = 350
    assert inv.moving_average_cost(db, setup["water"], today()) == Decimal(350)


def test_disposal_pending_then_approve(db, business, setup):
    _buy(db, business, setup)
    d = _dispose(db, business, setup)
    assert d.status == DisposalStatus.PENDING and d.number.startswith("D-")
    # 대기 중에는 재고는 그대로, 가용 재고만 줄어든다
    row = inv.stock_rows(db, business.id, [setup["water"]], None, today(), today())[0]
    assert (row.qty, row.pending, row.available) == (200, 5, 195)

    inv.approve_disposal(db, business, setup["user"], d)
    db.commit()
    assert (d.unit_cost, d.loss_amount) == (300, 1500)
    assert _stock(db, business, setup) == 195
    row = inv.stock_rows(db, business.id, [setup["water"]], None, today(), today())[0]
    assert (row.qty, row.pending, row.disposed) == (195, 0, 5)
    assert _journal(db, d.journal_entry_id) == [
        ("재고자산폐기손실", 1500, 0),
        ("상품", 0, 1500),
    ]


def test_disposal_to_cogs_setting(db, business, setup):
    business.disposal_to_cogs = True
    _buy(db, business, setup)
    d = _dispose(db, business, setup)
    inv.approve_disposal(db, business, setup["user"], d)
    assert _journal(db, d.journal_entry_id)[0][0] == "상품매출원가"


def test_disposal_cannot_exceed_available(db, business, setup):
    _buy(db, business, setup, boxes=1)  # 20병
    _dispose(db, business, setup, qty=15)
    with pytest.raises(inv.InventoryError, match="가용 재고"):
        _dispose(db, business, setup, qty=6)


def test_unapprove_and_cancel_in_open_month(db, business, setup):
    _buy(db, business, setup)
    d = _dispose(db, business, setup)
    inv.approve_disposal(db, business, setup["user"], d)
    db.commit()
    entry_id = d.journal_entry_id
    inv.unapprove_disposal(db, business, d)
    db.commit()
    assert d.status == DisposalStatus.PENDING and _stock(db, business, setup) == 200
    assert db.get(JournalEntry, entry_id) is None
    inv.cancel_disposal(db, business, setup["user"], d)
    assert d.status == DisposalStatus.CANCELED


def test_cancel_in_closed_month_creates_reversal(db, business, setup):
    last_month = today().replace(day=1) - timedelta(days=1)
    _buy(db, business, setup, d=last_month)
    d = _dispose(db, business, setup, d=last_month)
    inv.approve_disposal(db, business, setup["user"], d)
    closing.close_month(db, business.id, closing.year_month(last_month), None)
    db.commit()
    rev = inv.cancel_disposal(db, business, setup["user"], d)
    db.commit()
    assert d.status == DisposalStatus.REVERSED
    assert (rev.qty, rev.loss_amount, rev.disposal_date) == (-5, -1500, today())
    assert _stock(db, business, setup) == 200
    assert _journal(db, rev.journal_entry_id) == [("재고자산폐기손실", 0, 1500), ("상품", 1500, 0)]


def test_stock_count_adjustment(db, business, setup):
    _buy(db, business, setup)  # 200병
    adj = inv.save_adjustment(
        db, business, setup["user"], today(), _wh(db).id, {setup["water"].id: Decimal(190)}
    )
    db.commit()
    line = adj.lines[0]
    assert (line.book_qty, line.counted_qty, line.diff_qty, line.amount) == (200, 190, -10, -3000)
    assert _stock(db, business, setup) == 190
    move = db.scalar(select(StockMove).where(StockMove.kind == StockMoveKind.ADJUST))
    assert move.qty == -10
    assert _journal(db, adj.journal_entry_id) == [("재고자산감모손실", 3000, 0), ("상품", 0, 3000)]

    inv.cancel_adjustment(db, business, setup["user"], adj)
    db.commit()
    assert _stock(db, business, setup) == 200


# ---------- 창고 이동 ----------


@pytest.fixture
def two_warehouses(db, business):
    a = _wh(db)
    b = Warehouse(business_id=business.id, name="매장 창고")
    db.add(b)
    db.commit()
    return a, b


def _at(db, business, s, wh):
    return inv.stock_balances(db, business.id, {(s["water"].id, wh.id)})[(s["water"].id, wh.id)]


def test_transfer_moves_stock_between_warehouses(db, business, setup, two_warehouses):
    a, b = two_warehouses
    _buy(db, business, setup)  # A에 200병
    t = inv.save_transfer(
        db, business, setup["user"], today(), a.id, b.id, {setup["water"].id: Decimal(10)}
    )
    db.commit()
    assert t.number.startswith("T-")
    assert (_at(db, business, setup, a), _at(db, business, setup, b)) == (190, 10)
    assert _stock(db, business, setup) == 200  # 전체 수량은 그대로
    moves = db.scalars(select(StockMove).where(StockMove.transfer_id == t.id)).all()
    assert sorted(m.qty for m in moves) == [-10, 10]
    assert all(m.unit_cost is None for m in moves)
    # 이동은 원가에 영향이 없다
    assert inv.moving_average_cost(db, setup["water"], today()) == 300

    inv.cancel_transfer(db, business, setup["user"], t)
    db.commit()
    assert (_at(db, business, setup, a), _at(db, business, setup, b)) == (200, 0)


def test_transfer_validation(db, business, setup, two_warehouses):
    a, b = two_warehouses
    _buy(db, business, setup, boxes=1)  # 20병
    water = setup["water"].id
    with pytest.raises(inv.InventoryError, match="같아요"):
        inv.save_transfer(db, business, setup["user"], today(), a.id, a.id, {water: Decimal(1)})
    with pytest.raises(inv.InventoryError, match="가용 재고가 20병뿐"):
        inv.save_transfer(db, business, setup["user"], today(), a.id, b.id, {water: Decimal(21)})
    with pytest.raises(inv.InventoryError, match="한 품목 이상"):
        inv.save_transfer(db, business, setup["user"], today(), a.id, b.id, {})


def test_cancel_transfer_blocked_when_moved_stock_was_used(db, business, setup, two_warehouses):
    a, b = two_warehouses
    _buy(db, business, setup)
    t = inv.save_transfer(
        db, business, setup["user"], today(), a.id, b.id, {setup["water"].id: Decimal(10)}
    )
    inv.create_disposal(db, business, setup["user"], today(), setup["water"].id, b.id,
                        Decimal(10), DisposalReason.DAMAGED)  # fmt: skip
    d = db.scalar(select(Disposal))
    inv.approve_disposal(db, business, setup["user"], d)
    db.commit()
    with pytest.raises(inv.InventoryError, match="재고가 모자라요"):
        inv.cancel_transfer(db, business, setup["user"], t)


def test_cancel_transfer_in_closed_month_creates_reverse_move(db, business, setup, two_warehouses):
    a, b = two_warehouses
    last_month = today().replace(day=1) - timedelta(days=1)
    _buy(db, business, setup, d=last_month)
    t = inv.save_transfer(
        db, business, setup["user"], last_month, a.id, b.id, {setup["water"].id: Decimal(10)}
    )
    closing.close_month(db, business.id, closing.year_month(last_month), None)
    db.commit()
    rev = inv.cancel_transfer(db, business, setup["user"], t)
    db.commit()
    assert (rev.from_warehouse_id, rev.to_warehouse_id, rev.transfer_date) == (b.id, a.id, today())
    assert (_at(db, business, setup, a), _at(db, business, setup, b)) == (200, 0)
