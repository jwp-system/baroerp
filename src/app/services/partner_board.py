"""거래처 상황판: 홈 화면을 거래처 하나로 좁혀 본다(이번 달 거래·잔액·연체·처리 대기·추이).

채권(매출)·채무(매입)는 역할별로 나뉘므로, 사용자에게 허락된 쪽(side)만 계산한다.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Item, Partner, Payment, TaxInvoice, Voucher, VoucherLine
from app.models.finance import (
    EFFECTIVE_PAYMENT_STATUSES,
    InvoiceDirection,
    PaymentKind,
    PaymentStatus,
)
from app.models.master import PartnerKind
from app.models.voucher import EFFECTIVE_STATUSES, VoucherKind
from app.services import payments
from app.services.tax_invoice import issue_targets

VOUCHER_KIND = {
    PaymentKind.RECEIPT: VoucherKind.SALES,
    PaymentKind.DISBURSEMENT: VoucherKind.PURCHASE,
}
DIRECTION = {
    PaymentKind.RECEIPT: InvoiceDirection.SALES,
    PaymentKind.DISBURSEMENT: InvoiceDirection.PURCHASE,
}
PARTNER_KINDS = {
    PaymentKind.RECEIPT: (PartnerKind.SALES, PartnerKind.BOTH),
    PaymentKind.DISBURSEMENT: (PartnerKind.PURCHASE, PartnerKind.BOTH),
}
TREND_MONTHS = 6
TOP_ITEMS = 5
RECENT = 8


def sides(allowed: set[str]) -> list[PaymentKind]:
    """매출·채권 메뉴가 있으면 매출 쪽, 매입·채무 메뉴가 있으면 매입 쪽을 본다."""
    out = []
    if allowed & {"sales", "receivables"}:
        out.append(PaymentKind.RECEIPT)
    if allowed & {"purchase", "payables"}:
        out.append(PaymentKind.DISBURSEMENT)
    return out


def visible(partner: Partner, side_list: list[PaymentKind]) -> bool:
    return any(partner.kind in PARTNER_KINDS[s] for s in side_list)


def periods(t: date) -> tuple[date, date, date]:
    """이번 달 1일, 지난달 1일, 지난달 같은 날(홈 화면과 같은 비교 기준)"""
    this_start = t.replace(day=1)
    last_end = this_start - timedelta(days=1)
    last_start = last_end.replace(day=1)
    return this_start, last_start, last_start.replace(day=min(t.day, last_end.day))


def _supply_by_partner(db, business_id, kind, start, end, partner_id=None) -> dict[int, Decimal]:
    stmt = (
        select(Voucher.partner_id, func.sum(Voucher.supply_amount))
        .where(
            Voucher.business_id == business_id,
            Voucher.kind == kind,
            Voucher.voucher_date >= start,
            Voucher.voucher_date <= end,
            Voucher.status.in_(EFFECTIVE_STATUSES),
        )
        .group_by(Voucher.partner_id)
    )
    if partner_id is not None:
        stmt = stmt.where(Voucher.partner_id == partner_id)
    return {pid: Decimal(v or 0) for pid, v in db.execute(stmt)}


# ---------- 목록 ----------


@dataclass
class SideSummary:
    month: Decimal = Decimal(0)  # 이번 달 공급가
    balance: Decimal = Decimal(0)  # 미수금·미지급금
    overdue_30: Decimal = Decimal(0)


@dataclass
class OverviewRow:
    partner: Partner
    side: dict[PaymentKind, SideSummary] = field(default_factory=dict)

    @property
    def month_total(self) -> Decimal:
        return sum((s.month for s in self.side.values()), Decimal(0))

    @property
    def balance_total(self) -> Decimal:
        return sum((s.balance for s in self.side.values()), Decimal(0))


def overview(
    db: Session, business_id: int, side_list: list[PaymentKind], t: date, q: str = ""
) -> list[OverviewRow]:
    """이번 달 거래가 있거나 잔액이 남은 거래처(이번 달 거래 많은 순)"""
    this_start, _, _ = periods(t)
    rows: dict[int, OverviewRow] = {}

    def row(partner: Partner, side: PaymentKind) -> SideSummary:
        r = rows.setdefault(partner.id, OverviewRow(partner))
        return r.side.setdefault(side, SideSummary())

    for side in side_list:
        month = _supply_by_partner(db, business_id, VOUCHER_KIND[side], this_start, t)
        partners = {p.id: p for p in db.scalars(select(Partner).where(Partner.id.in_(month)))}
        for pid, amount in month.items():
            row(partners[pid], side).month = amount
        for b in payments.balances(db, business_id, side, t):
            if b.balance:
                s = row(b.partner, side)
                s.balance, s.overdue_30 = b.balance, b.overdue_30
    out = list(rows.values())
    if q:
        out = [r for r in out if q in r.partner.name or q in (r.partner.biz_no or "")]
    return sorted(out, key=lambda r: (-r.month_total, -r.balance_total, r.partner.name))


# ---------- 거래처 하나 ----------


@dataclass
class SideBoard:
    kind: PaymentKind
    now: Decimal
    prev: Decimal
    balance: payments.Balance | None
    due_week: Decimal
    open_count: int


def _side_board(db, business_id, partner, side, t) -> SideBoard:
    this_start, last_start, last_same = periods(t)
    vkind = VOUCHER_KIND[side]
    now = _supply_by_partner(db, business_id, vkind, this_start, t, partner.id)
    prev = _supply_by_partner(db, business_id, vkind, last_start, last_same, partner.id)
    balance = next((b for b in payments.balances(db, business_id, side, t)
                    if b.partner.id == partner.id), None)  # fmt: skip
    week_end = t + timedelta(days=6 - t.weekday())
    open_vouchers = payments.open_vouchers(
        db, business_id, side, partner.id, statuses=EFFECTIVE_PAYMENT_STATUSES
    )
    due_week = sum(
        (rest for v, rest in open_vouchers if payments.due_date(v) <= week_end), Decimal(0)
    )
    return SideBoard(
        kind=side,
        now=now.get(partner.id, Decimal(0)),
        prev=prev.get(partner.id, Decimal(0)),
        balance=balance,
        due_week=due_week,
        open_count=len(open_vouchers),
    )


def _month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def trend(db, business_id, partner_id, side_list, t) -> list[dict]:
    """최근 몇 달의 월별 매출·매입 공급가(이번 달 포함)"""
    start = t.replace(day=1)
    for _ in range(TREND_MONTHS - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    months: dict[str, dict] = {}
    d = start
    while d <= t:
        months[_month_key(d)] = {"month": _month_key(d), **{s: Decimal(0) for s in side_list}}
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    kinds = {VOUCHER_KIND[s]: s for s in side_list}
    stmt = select(Voucher.voucher_date, Voucher.kind, Voucher.supply_amount).where(
        Voucher.business_id == business_id,
        Voucher.partner_id == partner_id,
        Voucher.kind.in_(kinds),
        Voucher.voucher_date >= start,
        Voucher.voucher_date <= t,
        Voucher.status.in_(EFFECTIVE_STATUSES),
    )
    for vdate, kind, amount in db.execute(stmt):
        months[_month_key(vdate)][kinds[VoucherKind(kind)]] += Decimal(amount)
    return list(months.values())


def top_items(db, business_id, partner_id, side, t) -> list[tuple[Item, Decimal, Decimal]]:
    """최근 3개월 많이 거래한 품목(공급가 순): (품목, 재고 단위 수량, 공급가)"""
    start = (t.replace(day=1) - timedelta(days=62)).replace(day=1)
    rows = db.execute(
        select(Item, func.sum(VoucherLine.stock_qty), func.sum(VoucherLine.supply_amount))
        .join(VoucherLine, VoucherLine.item_id == Item.id)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(
            Voucher.business_id == business_id,
            Voucher.partner_id == partner_id,
            Voucher.kind == VOUCHER_KIND[side],
            Voucher.voucher_date >= start,
            Voucher.voucher_date <= t,
            Voucher.status.in_(EFFECTIVE_STATUSES),
        )
        .group_by(Item)
        .order_by(func.sum(VoucherLine.supply_amount).desc())
        .limit(TOP_ITEMS)
    ).all()
    return [(item, Decimal(qty or 0), Decimal(amount or 0)) for item, qty, amount in rows]


def recent(db, business_id, partner_id, side_list) -> list[tuple[date, object]]:
    """최근 전표·수금·지급(날짜 내림차순)"""
    vouchers = db.scalars(
        select(Voucher)
        .where(
            Voucher.business_id == business_id,
            Voucher.partner_id == partner_id,
            Voucher.kind.in_([VOUCHER_KIND[s] for s in side_list]),
        )  # fmt: skip
        .order_by(Voucher.voucher_date.desc(), Voucher.id.desc())
        .limit(RECENT)
    )
    pays = db.scalars(
        select(Payment)
        .where(
            Payment.business_id == business_id,
            Payment.partner_id == partner_id,
            Payment.kind.in_(side_list),
        )  # fmt: skip
        .order_by(Payment.payment_date.desc(), Payment.id.desc())
        .limit(RECENT)
    )
    items = [(v.voucher_date, v) for v in vouchers] + [(p.payment_date, p) for p in pays]
    return sorted(items, key=lambda x: (x[0], x[1].id), reverse=True)[:RECENT]


def pending(db, business_id, partner, side_list) -> dict[str, int]:
    out = {}
    if PaymentKind.DISBURSEMENT in side_list:
        out["payments"] = db.scalar(
            select(func.count()).where(
                Payment.business_id == business_id,
                Payment.partner_id == partner.id,
                Payment.status == PaymentStatus.PENDING,
            )
        ) or 0  # fmt: skip
    if PaymentKind.RECEIPT in side_list:
        out["unissued"] = sum(
            1 for v in issue_targets(db, business_id) if v.partner_id == partner.id
        )
    if partner.biz_no:
        out["unmatched"] = db.scalar(
            select(func.count()).where(
                TaxInvoice.business_id == business_id,
                TaxInvoice.counterparty_biz_no == partner.biz_no,
                TaxInvoice.direction.in_([DIRECTION[s] for s in side_list]),
                TaxInvoice.matched_at.is_(None),
            )
        ) or 0  # fmt: skip
    return out


def board(db: Session, business_id: int, partner: Partner, side_list, t: date) -> dict:
    shown = [s for s in side_list if partner.kind in PARTNER_KINDS[s]]
    return {
        "sides": [_side_board(db, business_id, partner, s, t) for s in shown],
        "trend": trend(db, business_id, partner.id, shown, t),
        "top": {s: top_items(db, business_id, partner.id, s, t) for s in shown},
        "recent": recent(db, business_id, partner.id, shown),
        "pending": pending(db, business_id, partner, shown),
    }
