"""수금·지급, 배분, 미수 잔액·연체, 지급 승인. 데모 시나리오 7번(수금 → 미수금 감소)."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.models import Role
from app.models.finance import PaymentKind, PaymentMethod, PaymentStatus
from app.services import closing
from app.services import payments as pay
from app.services.voucher import VoucherError, cancel_voucher
from app.timeutil import today
from tests.test_vouchers import _buy, _sell

R, W = PaymentKind.RECEIPT, PaymentKind.DISBURSEMENT


def _receive(db, business, s, amount, allocations=None, user=None, d=None):
    p = pay.save_payment(
        db, business, user or s["user"], R, d or today(), s["customer"].id, Decimal(amount),
        PaymentMethod.TRANSFER, allocations=allocations,
    )  # fmt: skip
    db.commit()
    return p


def _balance(db, business, kind, partner):
    return next(b for b in pay.balances(db, business.id, kind) if b.partner.id == partner.id)


def test_receipt_auto_allocates_oldest_first(db, business, setup):
    _buy(db, business, setup)
    v1 = _sell(db, business, setup, 10, d=today() - timedelta(days=1))  # 6,600원
    v2 = _sell(db, business, setup, 10)  # 6,600원
    p = _receive(db, business, setup, 10000)
    assert p.number.startswith("R-") and p.status == PaymentStatus.CONFIRMED
    assert [(a.voucher_id, a.amount) for a in p.allocations] == [(v1.id, 6600), (v2.id, 3400)]
    b = _balance(db, business, R, setup["customer"])
    assert (b.billed, b.paid, b.balance) == (13200, 10000, 3200)
    rest = pay.open_vouchers(db, business.id, R, setup["customer"].id)
    assert [(v.id, r) for v, r in rest] == [(v2.id, 3200)]


def test_manual_allocation_limits(db, business, setup):
    _buy(db, business, setup)
    v = _sell(db, business, setup, 10)
    with pytest.raises(pay.PaymentError, match="남은 금액"):
        _receive(db, business, setup, 10000, allocations={v.id: Decimal(7000)})
    db.rollback()
    with pytest.raises(pay.PaymentError, match="배분 합계"):
        _receive(db, business, setup, 1000, allocations={v.id: Decimal(2000)})
    db.rollback()
    # 일부만 배분하고 나머지는 선수금
    p = _receive(db, business, setup, 8000, allocations={v.id: Decimal(6600)})
    assert p.allocated == 6600
    assert _balance(db, business, R, setup["customer"]).balance == -1400


def test_aging_buckets(db, business, setup):
    setup["customer"].payment_terms_days = 30
    _buy(db, business, setup)
    _sell(db, business, setup, 10, d=today() - timedelta(days=75))  # 45일 연체
    _sell(db, business, setup, 10)  # 미도래
    b = _balance(db, business, R, setup["customer"])
    assert b.buckets["d60"] == 6600 and b.buckets["current"] == 6600
    assert b.overdue_30 == 6600 and b.max_overdue == 45


def test_paid_voucher_cannot_be_canceled(db, business, setup):
    _buy(db, business, setup)
    v = _sell(db, business, setup, 10)
    p = _receive(db, business, setup, 1000)
    with pytest.raises(VoucherError, match="수금된 전표"):
        cancel_voucher(db, business, setup["user"], v)
    db.rollback()
    pay.cancel_payment(db, business, setup["user"], p)
    db.commit()
    cancel_voucher(db, business, setup["user"], v)


def test_disbursement_over_threshold_needs_owner_approval(db, business, setup, make_user):
    business.payment_approval_threshold = 50000
    v = _buy(db, business, setup)  # 66,000원
    p = pay.save_payment(
        db, business, setup["user"], W, today(), setup["supplier"].id, Decimal(66000),
        PaymentMethod.TRANSFER,
    )  # fmt: skip
    db.commit()
    assert p.number.startswith("W-") and p.status == PaymentStatus.PENDING
    # 승인 전에는 미지급금이 그대로지만, 같은 전표에 또 배분할 수는 없다
    assert _balance(db, business, W, setup["supplier"]).balance == 66000
    assert pay.open_vouchers(db, business.id, W, setup["supplier"].id) == []
    with pytest.raises(pay.PaymentError, match="대표만"):
        pay.approve_payment(db, business, setup["user"], p)
    owner = make_user(Role.OWNER)
    pay.approve_payment(db, business, owner, p)
    db.commit()
    assert _balance(db, business, W, setup["supplier"]).balance == 0
    assert v.id == p.allocations[0].voucher_id


def test_cancel_in_closed_month_creates_negative_payment(db, business, setup):
    last_month = today().replace(day=1) - timedelta(days=1)
    _buy(db, business, setup, d=last_month)
    _sell(db, business, setup, 10, d=last_month)
    p = _receive(db, business, setup, 6600, d=last_month)
    closing.close_month(db, business.id, closing.year_month(last_month), None)
    db.commit()
    rev = pay.cancel_payment(db, business, setup["user"], p)
    db.commit()
    assert p.status == PaymentStatus.REVERSED and rev.amount == -6600
    assert _balance(db, business, R, setup["customer"]).balance == 6600
    assert len(pay.open_vouchers(db, business.id, R, setup["customer"].id)) == 1


def test_ledger_running_balance(db, business, setup):
    _buy(db, business, setup)
    _sell(db, business, setup, 10, d=today() - timedelta(days=40))
    _sell(db, business, setup, 10)
    _receive(db, business, setup, 5000)
    opening, rows = pay.ledger(
        db, business.id, R, setup["customer"].id, today() - timedelta(days=10), today()
    )
    assert opening == 6600
    assert [(r.kind, r.balance) for r in rows] == [("voucher", 13200), ("payment", 8200)]
