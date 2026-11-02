"""거래처 상황판: 거래처별 이번 달 거래·잔액·연체 목록과, 거래처 하나의 홈 화면."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import Forbidden, require_menu
from app.db import get_db
from app.models import Partner, User
from app.models.finance import PaymentKind
from app.services import partner_board as svc
from app.services.master import format_qty
from app.templating import render
from app.timeutil import today

router = APIRouter(prefix="/partners", include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]
BoardUser = Annotated[User, require_menu("partners")]

LABELS = {
    PaymentKind.RECEIPT: {"billed": "매출", "balance": "미수금", "paid": "수금",
                          "ledger": "/receivables", "voucher": "/sales", "menu": "receivables",
                          "voucher_menu": "sales"},
    PaymentKind.DISBURSEMENT: {"billed": "매입", "balance": "미지급금", "paid": "지급",
                               "ledger": "/payables", "voucher": "/purchase", "menu": "payables",
                               "voucher_menu": "purchase"},
}  # fmt: skip


def _sides(request: Request) -> list[PaymentKind]:
    sides = svc.sides(request.state.allowed)
    if not sides:  # 매출·매입 어느 쪽 권한도 없으면 볼 거래처가 없다
        raise Forbidden()
    return sides


@router.get("")
def index(request: Request, db: DB, _user: BoardUser, q: str = ""):
    sides = _sides(request)
    rows = svc.overview(db, request.state.business.id, sides, today(), q.strip())
    ctx = {"active": "partners", "rows": rows, "sides": sides, "labels": LABELS, "q": q}
    template = "partners/_table.html" if request.headers.get("HX-Target") else "partners/index.html"
    return render(request, template, ctx)


@router.get("/{partner_id}")
def board(request: Request, db: DB, _user: BoardUser, partner_id: int):
    sides = _sides(request)
    business_id = request.state.business.id
    partner = db.get(Partner, partner_id)
    if partner is None or partner.business_id != business_id or not svc.visible(partner, sides):
        return RedirectResponse("/partners", status_code=303)
    t = today()
    ctx = {
        "active": "partners",
        "partner": partner,
        "labels": LABELS,
        "today": t,
        "fmt_qty": format_qty,
        **svc.board(db, business_id, partner, sides, t),
    }
    return render(request, "partners/board.html", ctx)
