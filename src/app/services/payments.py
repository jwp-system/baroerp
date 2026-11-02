"""수금·지급과 채권·채무. 수금·지급 금액은 전표에 배분하고, 잔액·연체는 배분 기준으로 본다.

- 미수(미지급) 잔액 = 장부에 남는 전표 합계 − 확정된 수금(지급) 합계
- 연체 일수 = 오늘 − (전표 일자 + 거래처 결제조건 일수), 전표별 미배분 금액 기준
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Business, Partner, Payment, PaymentAllocation, Role, User, Voucher
from app.models.finance import (
    EFFECTIVE_PAYMENT_STATUSES,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from app.models.master import PartnerKind
from app.models.voucher import EFFECTIVE_STATUSES, VoucherKind, VoucherStatus
from app.services import closing, numbering
from app.timeutil import today

_PREFIX = {PaymentKind.RECEIPT: "R", PaymentKind.DISBURSEMENT: "W"}
VOUCHER_KIND = {
    PaymentKind.RECEIPT: VoucherKind.SALES,
    PaymentKind.DISBURSEMENT: VoucherKind.PURCHASE,
}
PARTNER_KINDS = {
    PaymentKind.RECEIPT: (PartnerKind.SALES, PartnerKind.BOTH),
    PaymentKind.DISBURSEMENT: (PartnerKind.PURCHASE, PartnerKind.BOTH),
}
# 배분 한도 계산에는 승인 대기 지급도 포함(같은 전표에 이중 지급 방지)
_RESERVING = (*EFFECTIVE_PAYMENT_STATUSES, PaymentStatus.PENDING)
AGING_BUCKETS = [("current", "미도래"), ("d30", "1~30일"), ("d60", "31~60일"),
                 ("d90", "61~90일"), ("over", "90일 초과")]  # fmt: skip


class PaymentError(ValueError):
    pass


def allocated_by_voucher(db: Session, voucher_ids, statuses=_RESERVING) -> dict[int, Decimal]:
    if not voucher_ids:
        return {}
    rows = db.execute(
        select(PaymentAllocation.voucher_id, func.sum(PaymentAllocation.amount))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(PaymentAllocation.voucher_id.in_(list(voucher_ids)), Payment.status.in_(statuses))
        .group_by(PaymentAllocation.voucher_id)
    )
    return {v: Decimal(a or 0) for v, a in rows}


def open_vouchers(
    db: Session, business_id: int, kind: PaymentKind, partner_id: int | None = None,
    statuses=_RESERVING,
) -> list[tuple[Voucher, Decimal]]:  # fmt: skip
    """배분할 금액이 남은 전표(오래된 순)와 남은 금액"""
    stmt = (
        select(Voucher)
        .where(
            Voucher.business_id == business_id,
            Voucher.kind == VOUCHER_KIND[kind],
            Voucher.status == VoucherStatus.CONFIRMED,
            Voucher.reversal_of_id.is_(None),
            Voucher.total_amount > 0,
        )
        .options(selectinload(Voucher.partner))
        .order_by(Voucher.voucher_date, Voucher.id)
    )
    if partner_id is not None:
        stmt = stmt.where(Voucher.partner_id == partner_id)
    vouchers = list(db.scalars(stmt))
    done = allocated_by_voucher(db, [v.id for v in vouchers], statuses)
    out = []
    for v in vouchers:
        rest = v.total_amount - done.get(v.id, Decimal(0))
        if rest > 0:
            out.append((v, rest))
    return out


# ---------- 등록 ----------


def save_payment(
    db: Session,
    business: Business,
    user: User,
    kind: PaymentKind,
    payment_date: date,
    partner_id: int,
    amount: Decimal,
    method: PaymentMethod,
    account_ref: str = "",
    memo: str = "",
    allocations: dict[int, Decimal] | None = None,
) -> Payment:
    """allocations를 비우면 오래된 전표부터 자동 배분한다. 남는 금액은 선수금(선급금)으로 둔다."""
    closing.ensure_open(db, business.id, payment_date)
    partner = db.get(Partner, partner_id)
    if partner is None or partner.business_id != business.id:
        raise PaymentError("거래처를 골라 주세요")
    if partner.kind not in PARTNER_KINDS[kind]:
        label = "매출" if kind == PaymentKind.RECEIPT else "매입"
        raise PaymentError(f"'{partner.name}'은(는) {label} 거래처가 아니에요")
    if amount <= 0:
        raise PaymentError("금액은 0보다 커야 해요")
    if amount != amount.to_integral_value():
        raise PaymentError("금액은 원 단위로 입력해 주세요")

    candidates = {v.id: rest for v, rest in open_vouchers(db, business.id, kind, partner.id)}
    plan: list[tuple[int, Decimal]] = []
    if allocations:
        for vid, amt in allocations.items():
            if amt <= 0:
                continue
            if vid not in candidates:
                raise PaymentError("배분할 수 없는 전표가 있어요. 화면을 새로 고쳐 주세요")
            if amt > candidates[vid]:
                v = db.get(Voucher, vid)
                raise PaymentError(f"{v.number}에 남은 금액({candidates[vid]:,.0f}원)보다 많아요")
            plan.append((vid, amt))
        if sum((a for _, a in plan), Decimal(0)) > amount:
            raise PaymentError("배분 합계가 금액보다 커요")
    else:
        left = amount
        for vid, rest in candidates.items():
            if left <= 0:
                break
            take = min(left, rest)
            plan.append((vid, take))
            left -= take

    needs_approval = (
        kind == PaymentKind.DISBURSEMENT
        and amount >= business.payment_approval_threshold
        and user.role != Role.OWNER
    )
    payment = Payment(
        business_id=business.id,
        kind=kind,
        number=numbering.next_number(db, Payment, business.id, _PREFIX[kind], payment_date),
        payment_date=payment_date,
        partner_id=partner.id,
        amount=amount,
        method=method,
        account_ref=account_ref.strip() or None,
        memo=memo.strip() or None,
        status=PaymentStatus.PENDING if needs_approval else PaymentStatus.CONFIRMED,
        created_by_id=user.id,
    )
    if not needs_approval and kind == PaymentKind.DISBURSEMENT:
        payment.approved_by_id = user.id
        payment.approved_at = datetime.now(UTC)
    for vid, amt in plan:
        payment.allocations.append(
            PaymentAllocation(business_id=business.id, voucher_id=vid, amount=amt)
        )
    db.add(payment)
    db.flush()
    return payment


def approve_payment(db: Session, business: Business, user: User, p: Payment) -> None:
    if user.role != Role.OWNER:
        raise PaymentError("지급 승인은 대표만 할 수 있어요")
    if p.status != PaymentStatus.PENDING:
        raise PaymentError("승인 대기 중인 지급만 승인할 수 있어요")
    closing.ensure_open(db, business.id, p.payment_date)
    p.status = PaymentStatus.CONFIRMED
    p.approved_by_id = user.id
    p.approved_at = datetime.now(UTC)


def reject_payment(db: Session, user: User, p: Payment) -> None:
    if user.role != Role.OWNER:
        raise PaymentError("지급 반려는 대표만 할 수 있어요")
    if p.status != PaymentStatus.PENDING:
        raise PaymentError("승인 대기 중인 지급만 반려할 수 있어요")
    p.status = PaymentStatus.REJECTED
    p.approved_by_id = user.id
    p.approved_at = datetime.now(UTC)


def cancel_payment(db: Session, business: Business, user: User, p: Payment) -> Payment | None:
    """마감 전·승인 대기는 취소, 마감된 달은 오늘 날짜로 음수 취소 건"""
    if p.reversal_of_id:
        raise PaymentError("취소 건은 다시 취소할 수 없어요")
    if p.status == PaymentStatus.PENDING:
        p.status = PaymentStatus.CANCELED
        return None
    if p.status != PaymentStatus.CONFIRMED:
        raise PaymentError("이미 취소되었거나 반려된 건이에요")
    if not closing.is_closed(db, business.id, p.payment_date):
        p.status = PaymentStatus.CANCELED
        return None
    rev_date = today()
    closing.ensure_open(db, business.id, rev_date)
    kind = PaymentKind(p.kind)
    rev = Payment(
        business_id=business.id,
        kind=kind,
        number=numbering.next_number(db, Payment, business.id, _PREFIX[kind], rev_date),
        payment_date=rev_date,
        partner_id=p.partner_id,
        amount=-p.amount,
        method=p.method,
        account_ref=p.account_ref,
        memo=f"{p.number} 취소",
        created_by_id=user.id,
        reversal_of_id=p.id,
    )
    for a in p.allocations:
        rev.allocations.append(
            PaymentAllocation(business_id=business.id, voucher_id=a.voucher_id, amount=-a.amount)
        )
    db.add(rev)
    p.status = PaymentStatus.REVERSED
    db.flush()
    return rev


def voucher_allocated(db: Session, voucher_id: int) -> Decimal:
    """전표에 배분된 금액(승인 대기 포함)"""
    return allocated_by_voucher(db, [voucher_id]).get(voucher_id, Decimal(0))


# ---------- 채권·채무 ----------


@dataclass
class Balance:
    partner: Partner
    billed: Decimal = Decimal(0)  # 전표 합계
    paid: Decimal = Decimal(0)  # 수금·지급 합계
    buckets: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    max_overdue: int = 0

    @property
    def balance(self) -> Decimal:
        return self.billed - self.paid

    @property
    def overdue_30(self) -> Decimal:
        return sum((self.buckets[k] for k in ("d60", "d90", "over")), Decimal(0))


def _bucket(days: int) -> str:
    if days <= 0:
        return "current"
    if days <= 30:
        return "d30"
    if days <= 60:
        return "d60"
    if days <= 90:
        return "d90"
    return "over"


def due_date(v: Voucher) -> date:
    return v.voucher_date + timedelta(days=v.partner.payment_terms_days or 0)


def balances(db: Session, business_id: int, kind: PaymentKind, as_of: date | None = None):
    """거래처별 잔액과 연체 구간(잔액이 있거나 이번에 거래가 있었던 거래처)"""
    as_of = as_of or today()
    out: dict[int, Balance] = {}

    def row(partner: Partner) -> Balance:
        if partner.id not in out:
            out[partner.id] = Balance(partner=partner)
        return out[partner.id]

    billed = db.execute(
        select(Partner, func.sum(Voucher.total_amount))
        .join(Voucher, Voucher.partner_id == Partner.id)
        .where(
            Voucher.business_id == business_id,
            Voucher.kind == VOUCHER_KIND[kind],
            Voucher.status.in_(EFFECTIVE_STATUSES),
        )
        .group_by(Partner)
    )
    for partner, total in billed:
        row(partner).billed = Decimal(total or 0)
    paid = db.execute(
        select(Partner, func.sum(Payment.amount))
        .join(Payment, Payment.partner_id == Partner.id)
        .where(
            Payment.business_id == business_id,
            Payment.kind == kind,
            Payment.status.in_(EFFECTIVE_PAYMENT_STATUSES),
        )
        .group_by(Partner)
    )
    for partner, total in paid:
        row(partner).paid = Decimal(total or 0)

    for v, rest in open_vouchers(db, business_id, kind, statuses=EFFECTIVE_PAYMENT_STATUSES):
        b = row(v.partner)
        days = (as_of - due_date(v)).days
        b.buckets[_bucket(days)] += rest
        b.max_overdue = max(b.max_overdue, days)
    # 배분되지 않은 수금(선수금)은 가장 최근 구간에서 뺀다
    for b in out.values():
        unallocated = sum(b.buckets.values(), Decimal(0)) - b.balance
        for key, _ in AGING_BUCKETS:
            if unallocated <= 0:
                break
            take = min(unallocated, b.buckets[key])
            b.buckets[key] -= take
            unallocated -= take
    return sorted(out.values(), key=lambda b: (-b.balance, b.partner.name))


@dataclass
class LedgerRow:
    when: date
    kind: str  # voucher | payment
    obj: object
    increase: Decimal
    decrease: Decimal
    balance: Decimal = Decimal(0)


def ledger(
    db: Session, business_id: int, kind: PaymentKind, partner_id: int, start: date, end: date
) -> tuple[Decimal, list[LedgerRow]]:
    """거래처 원장: (기초 잔액, 기간 행[전표 +, 수금·지급 −, 누적 잔액])"""
    v_cond = [
        Voucher.business_id == business_id,
        Voucher.partner_id == partner_id,
        Voucher.kind == VOUCHER_KIND[kind],
        Voucher.status.in_(EFFECTIVE_STATUSES),
    ]
    p_cond = [
        Payment.business_id == business_id,
        Payment.partner_id == partner_id,
        Payment.kind == kind,
        Payment.status.in_(EFFECTIVE_PAYMENT_STATUSES),
    ]
    billed = db.scalar(
        select(func.sum(Voucher.total_amount)).where(*v_cond, Voucher.voucher_date < start)
    )
    paid = db.scalar(select(func.sum(Payment.amount)).where(*p_cond, Payment.payment_date < start))
    opening = Decimal(billed or 0) - Decimal(paid or 0)
    v_base = select(Voucher).where(*v_cond)
    p_base = select(Payment).where(*p_cond)
    rows = [
        LedgerRow(v.voucher_date, "voucher", v, v.total_amount, Decimal(0))
        for v in db.scalars(
            v_base.where(Voucher.voucher_date >= start, Voucher.voucher_date <= end)
        )
    ] + [
        LedgerRow(p.payment_date, "payment", p, Decimal(0), p.amount)
        for p in db.scalars(
            p_base.where(Payment.payment_date >= start, Payment.payment_date <= end)
        )
    ]
    rows.sort(key=lambda r: (r.when, 0 if r.kind == "voucher" else 1, r.obj.number))
    running = opening
    for r in rows:
        running += r.increase - r.decrease
        r.balance = running
    return opening, rows


def due_between(db: Session, business_id: int, kind: PaymentKind, start: date, end: date):
    """결제 예정일이 기간에 드는 미배분 금액 합계(이번 주 수금·지급 예정)"""
    total = Decimal(0)
    for v, rest in open_vouchers(db, business_id, kind, statuses=EFFECTIVE_PAYMENT_STATUSES):
        if start <= due_date(v) <= end:
            total += rest
    return total
