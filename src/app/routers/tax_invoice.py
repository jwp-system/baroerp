"""세금계산서: 발행 대상(미발행 알림), 홈택스 엑셀 업로드·전표 대사, 부가세 합계.
세무대리인은 마감된 달의 자료만 조회한다(기획안 2장)."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_menu
from app.db import get_db
from app.export import xlsx_response
from app.listing import paginate, pick_template
from app.models import Role, TaxInvoice, UploadLog, User
from app.models.finance import INVOICE_DIRECTION_LABELS, InvoiceDirection
from app.services import closing
from app.services import tax_invoice as svc
from app.templating import flash, render
from app.timeutil import today

router = APIRouter(prefix="/tax-invoice", include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]
TaxUser = Annotated[User, require_menu("tax_invoice")]
CAN_EDIT = {Role.OWNER, Role.ACCOUNTANT}
MAX_UPLOAD = 10 * 1024 * 1024


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _tabs(user: User) -> list[tuple[str, str, str]]:
    tabs = [
        ("targets", "발행 대상", "/tax-invoice"),
        ("list", "계산서 내역", "/tax-invoice/list"),
        ("upload", "홈택스 업로드", "/tax-invoice/upload"),
        ("summary", "부가세 합계", "/tax-invoice/summary"),
    ]
    if user.role in CAN_EDIT:
        return tabs
    return [t for t in tabs if t[0] in ("list", "summary")]


def _ctx(user: User, tab: str, **kw) -> dict:
    return {"active": "tax_invoice", "tabs": _tabs(user), "tab": tab,
            "can_edit": user.role in CAN_EDIT, **kw}  # fmt: skip


def _closed_only(user: User) -> bool:
    return user.role == Role.TAX_AGENT


def _visible(db: Session, user: User, business_id: int, d: date) -> bool:
    return not _closed_only(user) or closing.is_closed(db, business_id, d)


def _closed_filter(db: Session, business_id: int, column):
    """마감된 달에 속하는지(세무대리인용)"""
    months = sorted(closing.closed_months(db, business_id))
    if not months:
        return false()
    conds = []
    for ym in months:
        start = date.fromisoformat(f"{ym}-01")
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        conds.append((column >= start) & (column < end))
    return or_(*conds)


# ---------- 발행 대상 ----------


@router.get("")
def targets(request: Request, db: DB, user: TaxUser):
    if user.role not in CAN_EDIT:
        return _redirect("/tax-invoice/list")
    rows = svc.issue_targets(db, request.state.business.id)
    t = today()
    ctx = _ctx(
        user,
        "targets",
        rows=rows,
        deadline=svc.issue_deadline,
        today=t,
        overdue=sum(1 for v in rows if svc.issue_deadline(v.voucher_date) < t),
        total=sum((v.total_amount for v in rows), Decimal(0)),
    )
    return render(request, "tax_invoice/targets.html", ctx)


@router.get("/targets.xlsx")
def targets_export(request: Request, db: DB, user: TaxUser):
    if user.role not in CAN_EDIT:
        return _redirect("/tax-invoice/list")
    rows = svc.issue_targets(db, request.state.business.id)
    columns = [
        ("작성일자", lambda v: v.voucher_date),
        ("발급기한", lambda v: svc.issue_deadline(v.voucher_date)),
        ("전표번호", lambda v: v.number),
        ("공급받는자 사업자번호", lambda v: v.partner.biz_no_display),
        ("상호", lambda v: v.partner.name),
        ("대표자", lambda v: v.partner.ceo_name),
        ("주소", lambda v: v.partner.address),
        ("업태", lambda v: v.partner.biz_type),
        ("종목", lambda v: v.partner.biz_item),
        ("이메일", lambda v: v.partner.email),
        (
            "품목",
            lambda v: (
                v.lines[0].item.name + (f" 외 {len(v.lines) - 1}건" if len(v.lines) > 1 else "")
            ),
        ),  # noqa: E501
        ("공급가액", lambda v: v.supply_amount),
        ("세액", lambda v: v.vat_amount),
        ("합계", lambda v: v.total_amount),
    ]
    return xlsx_response("세금계산서_발행대상", columns, rows)


# ---------- 계산서 내역 ----------


class InvoiceFilter:
    def __init__(
        self,
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
        direction: Annotated[str, Query()] = "",
        matched: Annotated[str, Query()] = "",
        q: Annotated[str, Query()] = "",
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        t = today()
        self.start = start or t.replace(month=1, day=1)
        self.end = end or t
        self.direction, self.matched, self.q, self.page = direction, matched, q.strip(), page

    def params(self) -> dict:
        return {"start": self.start, "end": self.end, "direction": self.direction,
                "matched": self.matched, "q": self.q}  # fmt: skip


def _invoice_query(db: Session, user: User, business_id: int, f: InvoiceFilter):
    stmt = (
        select(TaxInvoice)
        .where(
            TaxInvoice.business_id == business_id,
            TaxInvoice.issue_date >= f.start,
            TaxInvoice.issue_date <= f.end,
        )
        .options(selectinload(TaxInvoice.vouchers))
        .order_by(TaxInvoice.issue_date.desc(), TaxInvoice.id.desc())
    )
    if _closed_only(user):
        stmt = stmt.where(_closed_filter(db, business_id, TaxInvoice.issue_date))
    if f.direction:
        stmt = stmt.where(TaxInvoice.direction == f.direction)
    if f.matched == "1":
        stmt = stmt.where(TaxInvoice.matched_at.is_not(None))
    elif f.matched == "0":
        stmt = stmt.where(TaxInvoice.matched_at.is_(None))
    if f.q:
        stmt = stmt.where(
            or_(
                TaxInvoice.counterparty_name.contains(f.q),
                TaxInvoice.counterparty_biz_no.contains(f.q.replace("-", "")),
                TaxInvoice.approval_no.contains(f.q),
            )
        )
    return stmt


@router.get("/list")
def invoices(request: Request, db: DB, user: TaxUser, f: Annotated[InvoiceFilter, Depends()]):
    business_id = request.state.business.id
    page = paginate(db, _invoice_query(db, user, business_id, f), f.page)
    unmatched = db.scalar(
        select(func.count()).where(
            TaxInvoice.business_id == business_id, TaxInvoice.matched_at.is_(None)
        )
    )
    ctx = _ctx(
        user, "list", page=page, f=f, directions=INVOICE_DIRECTION_LABELS,
        unmatched=0 if _closed_only(user) else unmatched, closed_only=_closed_only(user),
    )  # fmt: skip
    return render(
        request, pick_template(request, "tax_invoice/list.html", "tax_invoice/_table.html"), ctx
    )


@router.get("/list.xlsx")
def invoices_export(
    request: Request, db: DB, user: TaxUser, f: Annotated[InvoiceFilter, Depends()]
):
    rows = db.scalars(_invoice_query(db, user, request.state.business.id, f))
    columns = [
        ("구분", lambda i: i.direction_label),
        ("작성일자", lambda i: i.issue_date),
        ("승인번호", lambda i: i.approval_no),
        ("거래처 사업자번호", lambda i: i.counterparty_biz_no_display),
        ("거래처", lambda i: i.counterparty_name),
        ("품목", lambda i: i.item_name),
        ("공급가액", lambda i: i.supply_amount),
        ("세액", lambda i: i.vat_amount),
        ("합계", lambda i: i.total_amount),
        ("대사 전표", lambda i: ", ".join(v.number for v in i.vouchers)),
    ]
    return xlsx_response("세금계산서_내역", columns, rows)


# ---------- 업로드 ----------


@router.get("/upload")
def upload_page(request: Request, db: DB, user: TaxUser):
    if user.role not in CAN_EDIT:
        return _redirect("/tax-invoice/list")
    logs = db.scalars(
        select(UploadLog)
        .where(UploadLog.business_id == request.state.business.id, UploadLog.source == "hometax")
        .options(selectinload(UploadLog.created_by))
        .order_by(UploadLog.id.desc())
        .limit(20)
    ).all()
    return render(request, "tax_invoice/upload.html", _ctx(user, "upload", logs=logs, result=None))


@router.post("/upload")
def upload(request: Request, db: DB, user: TaxUser, file: UploadFile):
    if user.role not in CAN_EDIT:
        return _redirect("/tax-invoice/list")
    data = file.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        flash(request, "파일은 10MB까지 올릴 수 있어요", "error")
        return _redirect("/tax-invoice/upload")
    log = svc.import_file(db, request.state.business, user, data, file.filename or "upload.xlsx")
    db.commit()
    logs = db.scalars(
        select(UploadLog)
        .where(UploadLog.business_id == request.state.business.id, UploadLog.source == "hometax")
        .order_by(UploadLog.id.desc())
        .limit(20)
    ).all()
    return render(request, "tax_invoice/upload.html", _ctx(user, "upload", logs=logs, result=log))


# ---------- 부가세 합계 ----------


def _periods(year: int) -> list[tuple[str, str, date, date]]:
    """(키, 이름, 시작, 끝): 분기(법인 예정·확정)와 반기(개인 확정)"""
    q = [(f"Q{i}", f"{i}분기", date(year, 3 * i - 2, 1),
          date(year + (i == 4), (3 * i) % 12 + 1, 1)) for i in range(1, 5)]  # fmt: skip
    h = [("H1", "1기(1~6월)", date(year, 1, 1), date(year, 7, 1)),
         ("H2", "2기(7~12월)", date(year, 7, 1), date(year + 1, 1, 1))]  # fmt: skip
    return q + h


@router.get("/summary")
def summary(request: Request, db: DB, user: TaxUser, year: int = 0, period: str = ""):
    business_id = request.state.business.id
    t = today()
    year = year or t.year
    periods = _periods(year)
    current = next((p for p in periods if p[0] == period), None) or next(
        p for p in periods if p[0].startswith("Q") and p[2] <= date(year, t.month, 1) < p[3]
    )
    stmt = select(TaxInvoice).where(
        TaxInvoice.business_id == business_id,
        TaxInvoice.issue_date >= current[2],
        TaxInvoice.issue_date < current[3],
    )
    if _closed_only(user):
        stmt = stmt.where(_closed_filter(db, business_id, TaxInvoice.issue_date))
    groups: dict[str, dict[str, dict]] = {d.value: {} for d in InvoiceDirection}
    for inv in db.scalars(stmt):
        g = groups[inv.direction].setdefault(
            inv.counterparty_biz_no,
            {"biz_no": inv.counterparty_biz_no_display, "name": inv.counterparty_name,
             "count": 0, "supply": Decimal(0), "vat": Decimal(0)},
        )  # fmt: skip
        g["count"] += 1
        g["supply"] += inv.supply_amount
        g["vat"] += inv.vat_amount
    totals = defaultdict(lambda: {"count": 0, "supply": Decimal(0), "vat": Decimal(0)})
    for direction, rows in groups.items():
        for g in rows.values():
            for k in ("count", "supply", "vat"):
                totals[direction][k] += g[k]
    ctx = _ctx(
        user, "summary", year=year, years=range(t.year, t.year - 6, -1), periods=periods,
        current=current, groups={k: sorted(v.values(), key=lambda g: -g["supply"])
                                 for k, v in groups.items()},
        totals=totals, directions=INVOICE_DIRECTION_LABELS, closed_only=_closed_only(user),
    )  # fmt: skip
    return render(request, "tax_invoice/summary.html", ctx)


# ---------- 상세·대사 ----------


@router.get("/{invoice_id}")
def detail(request: Request, db: DB, user: TaxUser, invoice_id: int):
    business_id = request.state.business.id
    inv = db.get(TaxInvoice, invoice_id)
    if inv is None or inv.business_id != business_id:
        return _redirect("/tax-invoice/list")
    if not _visible(db, user, business_id, inv.issue_date):
        return _redirect("/tax-invoice/list")
    candidates = (
        []
        if inv.matched_at
        else svc.unmatched_vouchers(db, business_id, inv.direction, inv.counterparty_biz_no)
    )
    prefix = "/sales" if inv.direction == InvoiceDirection.SALES else "/purchase"
    ctx = _ctx(user, "list", inv=inv, candidates=candidates, voucher_prefix=prefix)
    return render(request, "tax_invoice/view.html", ctx)


@router.post("/{invoice_id}/match")
def match(
    request: Request,
    db: DB,
    user: TaxUser,
    invoice_id: int,
    voucher_id: Annotated[list[int] | None, Form()] = None,
):
    business = request.state.business
    inv = db.get(TaxInvoice, invoice_id)
    if inv is None or inv.business_id != business.id or user.role not in CAN_EDIT:
        return _redirect("/tax-invoice/list")
    try:
        svc.match_manual(db, business, inv, voucher_id or [])
    except svc.InvoiceError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/tax-invoice/{inv.id}")
    db.commit()
    flash(request, "전표와 대사했어요")
    return _redirect(f"/tax-invoice/{inv.id}")


@router.post("/{invoice_id}/unmatch")
def unmatch(request: Request, db: DB, user: TaxUser, invoice_id: int):
    inv = db.get(TaxInvoice, invoice_id)
    if inv is None or inv.business_id != request.state.business.id or user.role not in CAN_EDIT:
        return _redirect("/tax-invoice/list")
    svc.unmatch(db, inv)
    db.commit()
    flash(request, "대사를 풀었어요")
    return _redirect(f"/tax-invoice/{inv.id}")
