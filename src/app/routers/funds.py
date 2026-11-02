"""자금: 수금·지급 등록(전표 배분), 입출금 내역, 지급 승인(기준금액 이상은 대표 승인)"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import check_approval_pin, require_menu
from app.db import get_db
from app.export import xlsx_response
from app.listing import paginate, pick_template
from app.models import Partner, Payment, PaymentAllocation, Role, User
from app.models.finance import (
    EFFECTIVE_PAYMENT_STATUSES,
    PAYMENT_KIND_LABELS,
    PAYMENT_METHOD_LABELS,
    PAYMENT_STATUS_LABELS,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from app.services import closing
from app.services import payments as svc
from app.templating import flash, render
from app.timeutil import today

router = APIRouter(prefix="/funds", include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]
FundsUser = Annotated[User, require_menu("funds")]
ERRORS = (svc.PaymentError, closing.ClosedPeriodError)

TABS = [
    ("list", "입출금 내역", "/funds"),
    ("approvals", "지급 승인", "/funds/approvals"),
]


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _ctx(tab: str, **kw) -> dict:
    return {"active": "funds", "tabs": TABS, "tab": tab, **kw}


def _owned(db: Session, payment_id: int, business_id: int) -> Payment | None:
    p = db.get(Payment, payment_id)
    return p if p is not None and p.business_id == business_id else None


def _pending_count(db: Session, business_id: int) -> int:
    return db.scalar(
        select(func.count()).where(
            Payment.business_id == business_id, Payment.status == PaymentStatus.PENDING
        )
    )


# ---------- 목록 ----------


class PaymentFilter:
    def __init__(
        self,
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
        q: Annotated[str, Query()] = "",
        kind: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        t = today()
        self.start, self.end = start or t.replace(day=1), end or t
        self.q, self.kind, self.status, self.page = q.strip(), kind, status, page

    def params(self) -> dict:
        return {"start": self.start, "end": self.end, "q": self.q, "kind": self.kind,
                "status": self.status}  # fmt: skip


def _query(business_id: int, f: PaymentFilter):
    stmt = (
        select(Payment)
        .join(Partner, Partner.id == Payment.partner_id)
        .where(
            Payment.business_id == business_id,
            Payment.payment_date >= f.start,
            Payment.payment_date <= f.end,
        )
        .options(selectinload(Payment.partner), selectinload(Payment.allocations))
        .order_by(Payment.payment_date.desc(), Payment.number.desc())
    )
    if f.q:
        stmt = stmt.where(or_(Partner.name.contains(f.q), Payment.number.contains(f.q)))
    if f.kind:
        stmt = stmt.where(Payment.kind == f.kind)
    if f.status:
        stmt = stmt.where(Payment.status == f.status)
    return stmt


@router.get("")
def index(request: Request, db: DB, _user: FundsUser, f: Annotated[PaymentFilter, Depends()]):
    business_id = request.state.business.id
    page = paginate(db, _query(business_id, f), f.page)
    totals = {k: Decimal(0) for k in PaymentKind}
    for p in db.scalars(_query(business_id, f)):
        if p.status in EFFECTIVE_PAYMENT_STATUSES:
            totals[PaymentKind(p.kind)] += p.amount
    ctx = _ctx(
        "list",
        page=page,
        f=f,
        totals=totals,
        kinds=PAYMENT_KIND_LABELS,
        statuses=PAYMENT_STATUS_LABELS,
        pending=_pending_count(db, business_id),
    )
    return render(request, pick_template(request, "funds/list.html", "funds/_table.html"), ctx)


@router.get("/export.xlsx")
def export(request: Request, db: DB, _user: FundsUser, f: Annotated[PaymentFilter, Depends()]):
    rows = db.scalars(_query(request.state.business.id, f))
    columns = [
        ("일자", lambda p: p.payment_date),
        ("번호", lambda p: p.number),
        ("구분", lambda p: p.kind_label),
        ("거래처", lambda p: p.partner.name),
        ("금액", lambda p: p.amount),
        ("배분", lambda p: p.allocated),
        ("수단", lambda p: p.method_label),
        ("계좌·카드", lambda p: p.account_ref),
        ("상태", lambda p: p.status_label),
        ("메모", lambda p: p.memo),
    ]
    return xlsx_response("입출금내역", columns, rows)


@router.get("/approvals")
def approvals(request: Request, db: DB, user: FundsUser):
    business_id = request.state.business.id
    rows = db.scalars(
        select(Payment)
        .where(Payment.business_id == business_id, Payment.status == PaymentStatus.PENDING)
        .options(selectinload(Payment.partner), selectinload(Payment.created_by))
        .order_by(Payment.payment_date, Payment.id)
    ).all()
    ctx = _ctx(
        "approvals",
        rows=rows,
        can_approve=user.role == Role.OWNER,
        threshold=request.state.business.payment_approval_threshold,
        pending=len(rows),
    )
    return render(request, "funds/approvals.html", ctx)


# ---------- 등록 ----------


def _partners(db: Session, business_id: int, kind: PaymentKind) -> list[Partner]:
    return list(
        db.scalars(
            select(Partner)
            .where(
                Partner.business_id == business_id,
                Partner.is_active,
                Partner.kind.in_(svc.PARTNER_KINDS[kind]),
            )
            .order_by(Partner.name)
        )
    )


def _kind(value: str) -> PaymentKind:
    return PaymentKind.DISBURSEMENT if value == PaymentKind.DISBURSEMENT else PaymentKind.RECEIPT


def _form_page(request, db, kind: PaymentKind, form: dict, error=None, status=200):
    business = request.state.business
    partner_id = int(form["partner_id"]) if str(form.get("partner_id", "")).isdigit() else None
    ctx = _ctx(
        "list",
        kind=kind.value,
        label=PAYMENT_KIND_LABELS[kind],
        partners=_partners(db, business.id, kind),
        methods=PAYMENT_METHOD_LABELS,
        form=form,
        error=error,
        open=svc.open_vouchers(db, business.id, kind, partner_id) if partner_id else [],
        partner_id=partner_id,
        alloc=form.get("alloc", {}),
        needs_approval=kind == PaymentKind.DISBURSEMENT,
        threshold=business.payment_approval_threshold,
        is_owner=request.state.user.role == Role.OWNER,
    )
    return render(request, "funds/form.html", ctx, status_code=status)


@router.get("/new")
def new(request: Request, db: DB, _user: FundsUser, kind: str = "receipt", partner_id: str = ""):
    form = {"payment_date": today().isoformat(), "partner_id": partner_id, "method": "transfer"}
    return _form_page(request, db, _kind(kind), form)


@router.get("/open-vouchers")
def open_vouchers(request: Request, db: DB, _user: FundsUser, kind: str = "", partner_id: str = ""):
    k = _kind(kind)
    rows = (
        svc.open_vouchers(db, request.state.business.id, k, int(partner_id))
        if partner_id.isdigit()
        else []
    )
    ctx = {"open": rows, "kind": k.value, "alloc": {}, "partner_id": partner_id or None}
    return render(request, "funds/_open_vouchers.html", ctx)


class PaymentForm(BaseModel):
    kind: str = "receipt"
    payment_date: date
    partner_id: str = ""
    amount: str = ""
    method: PaymentMethod = PaymentMethod.TRANSFER
    account_ref: str = ""
    memo: str = ""
    alloc_voucher_id: list[int] = []
    alloc_amount: list[str] = []


def _money(value: str, label: str) -> Decimal:
    try:
        return Decimal((value or "").replace(",", "").strip())
    except InvalidOperation as exc:
        raise svc.PaymentError(f"{label}은(는) 숫자여야 해요") from exc


@router.post("")
def create(request: Request, db: DB, user: FundsUser, form: Annotated[PaymentForm, Form()]):
    business = request.state.business
    kind = _kind(form.kind)
    alloc_raw = dict(zip(form.alloc_voucher_id, form.alloc_amount, strict=False))
    try:
        if not form.partner_id.isdigit():
            raise svc.PaymentError("거래처를 골라 주세요")
        allocations = {
            vid: _money(v, "배분 금액") for vid, v in alloc_raw.items() if v.strip()
        } or None
        p = svc.save_payment(
            db, business, user, kind, form.payment_date, int(form.partner_id),
            _money(form.amount, "금액"), form.method, form.account_ref, form.memo, allocations,
        )  # fmt: skip
    except ERRORS as exc:
        db.rollback()
        data = form.model_dump(mode="json") | {"alloc": alloc_raw}
        return _form_page(request, db, kind, data, str(exc), 400)
    db.commit()
    if p.status == PaymentStatus.PENDING:
        flash(request, f"지급을 등록했어요 · {p.number}. 기준금액 이상이라 대표 승인 후 반영돼요")
    else:
        flash(request, f"{p.kind_label}을 등록했어요 · {p.number}")
    return _redirect(f"/funds/{p.id}")


@router.get("/{payment_id}")
def detail(request: Request, db: DB, user: FundsUser, payment_id: int):
    business_id = request.state.business.id
    p = _owned(db, payment_id, business_id)
    if p is None:
        return _redirect("/funds")
    allocations = db.scalars(
        select(PaymentAllocation)
        .where(PaymentAllocation.payment_id == p.id)
        .options(selectinload(PaymentAllocation.voucher))
        .order_by(PaymentAllocation.id)
    ).all()
    ctx = _ctx(
        "approvals" if p.status == PaymentStatus.PENDING else "list",
        p=p,
        allocations=allocations,
        is_owner=user.role == Role.OWNER,
        locked=closing.is_closed(db, business_id, p.payment_date),
        reversal=db.scalar(select(Payment).where(Payment.reversal_of_id == p.id)),
        can_cancel_pending=user.role == Role.OWNER or p.created_by_id == user.id,
    )
    return render(request, "funds/view.html", ctx)


def _action(request, db, payment_id, action, done):
    p = _owned(db, payment_id, request.state.business.id)
    if p is None:
        return _redirect("/funds")
    try:
        result = action(p)
    except ERRORS as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/funds/{p.id}")
    db.commit()
    if isinstance(result, Payment):
        flash(request, f"마감된 달이라 취소 건 {result.number}를 만들었어요")
        return _redirect(f"/funds/{result.id}")
    flash(request, done)
    return _redirect(f"/funds/{p.id}")


@router.post("/{payment_id}/approve")
def approve(
    request: Request, db: DB, user: FundsUser, payment_id: int, pin: Annotated[str, Form()] = ""
):
    if error := check_approval_pin(db, user, pin):
        flash(request, error, "error")
        return _redirect(f"/funds/{payment_id}")
    business = request.state.business
    return _action(request, db, payment_id,
                   lambda p: svc.approve_payment(db, business, user, p),
                   "지급을 승인했어요. 미지급금에 반영했어요")  # fmt: skip


@router.post("/{payment_id}/reject")
def reject(request: Request, db: DB, user: FundsUser, payment_id: int):
    return _action(request, db, payment_id, lambda p: svc.reject_payment(db, user, p),
                   "지급을 반려했어요")  # fmt: skip


@router.post("/{payment_id}/cancel")
def cancel(request: Request, db: DB, user: FundsUser, payment_id: int):
    business = request.state.business
    p = _owned(db, payment_id, business.id)
    if (
        p is not None
        and p.status == PaymentStatus.PENDING
        and user.role != Role.OWNER
        and p.created_by_id != user.id
    ):
        flash(request, "승인 대기 중인 지급은 등록한 사람이나 대표만 취소할 수 있어요", "error")
        return _redirect(f"/funds/{p.id}")
    return _action(request, db, payment_id,
                   lambda p: svc.cancel_payment(db, business, user, p), "취소했어요")  # fmt: skip
