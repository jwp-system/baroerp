from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.db import get_db
from app.models import Disposal, Payment, TaxInvoice, Voucher
from app.models.finance import PaymentKind, PaymentStatus
from app.models.inventory import DisposalStatus
from app.models.voucher import EFFECTIVE_STATUSES, VoucherKind
from app.permissions import home_path
from app.services import payments
from app.services.tax_invoice import issue_targets
from app.templating import render
from app.timeutil import today

router = APIRouter(include_in_schema=False)
TOP_OVERDUE = 5


def _month_totals(db: Session, business_id: int, start: date, end: date) -> dict[str, Decimal]:
    """장부에 남는 전표의 공급가 합계(마감 후 취소는 원 전표와 취소 전표가 상계)"""
    rows = db.execute(
        select(Voucher.kind, func.sum(Voucher.supply_amount))
        .where(
            Voucher.business_id == business_id,
            Voucher.voucher_date >= start,
            Voucher.voucher_date <= end,
            Voucher.status.in_(EFFECTIVE_STATUSES),
        )
        .group_by(Voucher.kind)
    ).all()
    totals = {kind: Decimal(v or 0) for kind, v in rows}
    sales = totals.get(VoucherKind.SALES, Decimal(0))
    purchase = totals.get(VoucherKind.PURCHASE, Decimal(0))
    return {"sales": sales, "purchase": purchase, "profit": sales - purchase}


def _disposal_loss(db: Session, business_id: int, start: date, end: date) -> Decimal:
    return Decimal(
        db.scalar(
            select(func.sum(Disposal.loss_amount)).where(
                Disposal.business_id == business_id,
                Disposal.disposal_date >= start,
                Disposal.disposal_date <= end,
                Disposal.status.in_((DisposalStatus.APPROVED, DisposalStatus.REVERSED)),
            )
        )
        or 0
    )


def _count(db: Session, *conds) -> int:
    return db.scalar(select(func.count()).where(*conds)) or 0


@router.get("/")
def dashboard(request: Request, db: Annotated[Session, Depends(get_db)], user: CurrentUser):
    if "dashboard" not in request.state.allowed:
        return RedirectResponse(home_path(request.state.business, user.role), status_code=303)
    business_id = request.state.business.id
    t = today()
    this_start = t.replace(day=1)
    last_end = this_start - timedelta(days=1)
    last_start = last_end.replace(day=1)
    # 지난달은 같은 날짜까지 비교(11/2 기준이면 10/1~10/2)
    last_same = min(last_start.replace(day=min(t.day, last_end.day)), last_end)
    week_end = t + timedelta(days=6 - t.weekday())

    receivables = payments.balances(db, business_id, PaymentKind.RECEIPT)
    overdue = sorted((b for b in receivables if b.overdue_30 > 0), key=lambda b: -b.overdue_30)[
        :TOP_OVERDUE
    ]
    ctx = {
        "active": "dashboard",
        "now": _month_totals(db, business_id, this_start, t),
        "prev": _month_totals(db, business_id, last_start, last_same),
        "loss": _disposal_loss(db, business_id, this_start, t),
        "loss_prev": _disposal_loss(db, business_id, last_start, last_same),
        "overdue": overdue,
        "receivable_total": sum((b.balance for b in receivables), Decimal(0)),
        "due_in": payments.due_between(db, business_id, PaymentKind.RECEIPT, t, week_end),
        "due_out": payments.due_between(db, business_id, PaymentKind.DISBURSEMENT, t, week_end),
        "pending": {
            "payments": _count(
                db, Payment.business_id == business_id, Payment.status == PaymentStatus.PENDING
            ),
            "disposals": _count(
                db, Disposal.business_id == business_id, Disposal.status == DisposalStatus.PENDING
            ),
            "invoices_unissued": len(
                [v for v in issue_targets(db, business_id) if v.voucher_date < this_start]
            ),
            "invoices_unmatched": _count(
                db, TaxInvoice.business_id == business_id, TaxInvoice.matched_at.is_(None)
            ),
        },
    }
    return render(request, "dashboard.html", ctx)
