from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_menu
from app.db import get_db
from app.models import ClosingPeriod, Role, User, Voucher
from app.models.voucher import VoucherStatus
from app.services import closing
from app.templating import flash, render
from app.timeutil import today

router = APIRouter(prefix="/reports", include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]
ReportUser = Annotated[User, require_menu("reports")]

# 마감은 대표·경리, 마감 해제는 대표만(기획안 2장)
CAN_CLOSE = {Role.OWNER, Role.ACCOUNTANT}
CAN_REOPEN = {Role.OWNER}
MONTHS_SHOWN = 12


def _recent_months(n: int) -> list[str]:
    t = today()
    y, m = t.year, t.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def _valid_ym(ym: str) -> bool:
    try:
        date.fromisoformat(f"{ym}-01")
    except ValueError:
        return False
    return len(ym) == 7


@router.get("")
def index(request: Request, db: DB, user: ReportUser):
    business_id = request.state.business.id
    months = _recent_months(MONTHS_SHOWN)
    closed = {
        c.year_month: c
        for c in db.scalars(select(ClosingPeriod).where(ClosingPeriod.business_id == business_id))
    }
    counts = _voucher_counts(db, business_id, months)
    ctx = {
        "active": "reports",
        "months": months,
        "closed": closed,
        "counts": counts,
        "can_close": user.role in CAN_CLOSE,
        "can_reopen": user.role in CAN_REOPEN,
    }
    return render(request, "reports/index.html", ctx)


def _voucher_counts(db: Session, business_id: int, months: list[str]) -> dict[str, int]:
    start = date.fromisoformat(f"{months[-1]}-01")
    counts: dict[str, int] = {}
    for d in db.scalars(
        select(Voucher.voucher_date).where(
            Voucher.business_id == business_id,
            Voucher.voucher_date >= start,
            Voucher.status == VoucherStatus.CONFIRMED,
        )
    ):
        ym = closing.year_month(d)
        counts[ym] = counts.get(ym, 0) + 1
    return counts


@router.post("/closing/{ym}/close")
def close(request: Request, db: DB, user: ReportUser, ym: str):
    if user.role not in CAN_CLOSE or not _valid_ym(ym):
        flash(request, "마감 권한이 없어요", "error")
        return RedirectResponse("/reports", status_code=303)
    try:
        closing.close_month(db, request.state.business.id, ym, user.id)
    except closing.ClosedPeriodError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/reports", status_code=303)
    db.commit()
    flash(request, f"{ym} 마감했어요. 이제 이 달의 전표는 바꿀 수 없어요")
    return RedirectResponse("/reports", status_code=303)


@router.post("/closing/{ym}/reopen")
def reopen(request: Request, db: DB, user: ReportUser, ym: str):
    if user.role not in CAN_REOPEN or not _valid_ym(ym):
        flash(request, "마감 해제는 대표만 할 수 있어요", "error")
        return RedirectResponse("/reports", status_code=303)
    closing.reopen_month(db, request.state.business.id, ym)
    db.commit()
    flash(request, f"{ym} 마감을 풀었어요")
    return RedirectResponse("/reports", status_code=303)
