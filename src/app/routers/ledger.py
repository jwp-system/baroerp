"""채권(매출 거래처)·채무(매입 거래처): 거래처별 잔액·연체 구간(에이징)과 거래처 원장.
같은 규칙이므로 라우터 팩토리로 두 메뉴를 만든다."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_menu
from app.db import get_db
from app.export import xlsx_response
from app.models import Partner, User
from app.models.finance import EFFECTIVE_PAYMENT_STATUSES, PaymentKind
from app.models.master import PartnerKind
from app.services import payments as svc
from app.templating import render
from app.timeutil import today

DB = Annotated[Session, Depends(get_db)]


def make_router(kind: PaymentKind) -> APIRouter:
    receivable = kind == PaymentKind.RECEIPT
    menu = "receivables" if receivable else "payables"
    prefix = f"/{menu}"
    labels = {
        "title": "채권" if receivable else "채무",
        "balance": "미수금" if receivable else "미지급금",
        "billed": "매출" if receivable else "매입",
        "paid": "수금" if receivable else "지급",
        "voucher_prefix": "/sales" if receivable else "/purchase",
        "advance": "선수금" if receivable else "선급금",
    }
    router = APIRouter(prefix=prefix, include_in_schema=False)
    MenuUser = Annotated[User, require_menu(menu)]

    def ctx(**kw) -> dict:
        return {"active": menu, "kind": kind.value, "prefix": prefix, "labels": labels,
                "buckets": svc.AGING_BUCKETS, **kw}  # fmt: skip

    def rows_for(db, business_id, q: str, only_open: bool):
        rows = svc.balances(db, business_id, kind)
        if q:
            rows = [b for b in rows if q in b.partner.name or q in (b.partner.biz_no or "")]
        if only_open:
            rows = [b for b in rows if b.balance]
        return rows

    @router.get("")
    def index(request: Request, db: DB, _user: MenuUser, q: str = "", only_open: str = "1"):
        rows = rows_for(db, request.state.business.id, q.strip(), only_open == "1")
        totals = {
            "balance": sum((b.balance for b in rows), Decimal(0)),
            "overdue_30": sum((b.overdue_30 for b in rows), Decimal(0)),
            "buckets": {
                key: sum((b.buckets[key] for b in rows), Decimal(0)) for key, _ in svc.AGING_BUCKETS
            },
        }
        c = ctx(rows=rows, totals=totals, q=q, only_open=only_open)
        template = "ledger/_table.html" if request.headers.get("HX-Target") else "ledger/index.html"
        return render(request, template, c)

    @router.get("/export.xlsx")
    def export(request: Request, db: DB, _user: MenuUser, q: str = "", only_open: str = "1"):
        rows = rows_for(db, request.state.business.id, q.strip(), only_open == "1")
        columns = (
            [
                ("거래처", lambda b: b.partner.name),
                ("사업자번호", lambda b: b.partner.biz_no_display),
                (f"{labels['billed']} 합계", lambda b: b.billed),
                (f"{labels['paid']} 합계", lambda b: b.paid),
                (labels["balance"], lambda b: b.balance),
            ]
            + [(label, lambda b, k=key: b.buckets[k]) for key, label in svc.AGING_BUCKETS]
            + [
                ("최장 연체일", lambda b: max(b.max_overdue, 0)),
                ("결제조건(일)", lambda b: b.partner.payment_terms_days),
            ]
        )
        return xlsx_response(f"{labels['balance']}_에이징", columns, rows)

    @router.get("/{partner_id}")
    def partner_ledger(
        request: Request,
        db: DB,
        _user: MenuUser,
        partner_id: int,
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
    ):
        business_id = request.state.business.id
        partner = db.get(Partner, partner_id)
        if partner is None or partner.business_id != business_id:
            return RedirectResponse(prefix, status_code=303)
        t = today()
        end = end or t
        start = start or (end.replace(day=1) - timedelta(days=1)).replace(day=1)
        opening, rows = svc.ledger(db, business_id, kind, partner.id, start, end)
        closing_balance = rows[-1].balance if rows else opening
        # 공통 거래처는 반대편 잔액도 보여 주어 상계 판단을 돕는다
        other = None
        if partner.kind == PartnerKind.BOTH:
            other_kind = PaymentKind.DISBURSEMENT if receivable else PaymentKind.RECEIPT
            others = svc.balances(db, business_id, other_kind)
            other = next((b for b in others if b.partner.id == partner.id), None)
        c = ctx(
            partner=partner,
            start=start,
            end=end,
            opening=opening,
            rows=rows,
            closing_balance=closing_balance,
            open=svc.open_vouchers(
                db, business_id, kind, partner.id, statuses=EFFECTIVE_PAYMENT_STATUSES
            ),
            due_date=svc.due_date,
            today=t,
            other=other,
        )
        return render(request, "ledger/partner.html", c)

    return router


receivables_router = make_router(PaymentKind.RECEIPT)
payables_router = make_router(PaymentKind.DISBURSEMENT)
