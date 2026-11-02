"""매출·매입 전표 화면. 같은 규칙을 쓰므로 하나의 라우터 팩토리로 두 메뉴를 만든다."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_menu
from app.db import get_db
from app.export import xlsx_response
from app.listing import paginate, pick_template
from app.models import (
    Item,
    ItemUnit,
    Partner,
    TaxInvoice,
    Unit,
    User,
    Voucher,
    VoucherLine,
    Warehouse,
)
from app.models.finance import EFFECTIVE_PAYMENT_STATUSES
from app.models.master import PartnerKind
from app.models.voucher import (
    EFFECTIVE_STATUSES,
    VOUCHER_STATUS_LABELS,
    VoucherKind,
    VoucherStatus,
)
from app.services import closing
from app.services import master as master_svc
from app.services.payments import allocated_by_voucher
from app.services.voucher import LineInput, VoucherError, cancel_voucher, item_totals, save_voucher
from app.templating import flash, render
from app.timeutil import today

DB = Annotated[Session, Depends(get_db)]
LOOKUP_LIMIT = 20


class VoucherForm(BaseModel):
    voucher_date: date
    partner_id: str = ""
    memo: str = ""
    item_id: list[str] = []
    unit_id: list[str] = []
    qty: list[str] = []
    unit_price: list[str] = []
    warehouse_id: list[str] = []
    line_memo: list[str] = []


def _dec(value: str, label: str, no: int) -> Decimal:
    try:
        return Decimal((value or "0").replace(",", "").strip())
    except InvalidOperation as exc:
        raise VoucherError(f"{no}번째 줄: {label}은(는) 숫자여야 해요") from exc


def _parse_lines(form: VoucherForm) -> list[LineInput]:
    lines = []
    n = len(form.item_id)

    def at(values, i):
        return values[i] if i < len(values) else ""

    for i in range(n):
        if not form.item_id[i].isdigit():
            continue
        no = len(lines) + 1
        lines.append(
            LineInput(
                item_id=int(form.item_id[i]),
                unit_id=int(at(form.unit_id, i) or 0),
                qty=_dec(at(form.qty, i), "수량", no),
                unit_price=_dec(at(form.unit_price, i), "단가", no),
                warehouse_id=int(at(form.warehouse_id, i))
                if at(form.warehouse_id, i).isdigit()
                else None,
                memo=at(form.line_memo, i),
            )
        )
    return lines


class VoucherFilter:
    def __init__(
        self,
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
        q: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        t = today()
        self.start = start or t.replace(day=1)
        self.end = end or t
        self.q = q.strip()
        self.status = status
        self.page = page

    def params(self) -> dict:
        return {"start": self.start, "end": self.end, "q": self.q, "status": self.status}


def make_router(kind: VoucherKind) -> APIRouter:
    menu = kind.value if kind == VoucherKind.SALES else "purchase"
    prefix = "/sales" if kind == VoucherKind.SALES else "/purchase"
    label = "매출" if kind == VoucherKind.SALES else "매입"
    partner_kinds = (
        [PartnerKind.SALES, PartnerKind.BOTH]
        if kind == VoucherKind.SALES
        else [PartnerKind.PURCHASE, PartnerKind.BOTH]
    )
    router = APIRouter(prefix=prefix, include_in_schema=False)
    MenuUser = Annotated[User, require_menu(menu)]

    def ctx(**kw) -> dict:
        return {"active": menu, "kind": kind.value, "label": label, "prefix": prefix, **kw}

    def owned(db: Session, voucher_id: int, business_id: int) -> Voucher | None:
        v = db.get(Voucher, voucher_id)
        if v is None or v.business_id != business_id or v.kind != kind:
            return None
        return v

    # ---------- 목록 ----------

    def query(business_id: int, f: VoucherFilter):
        stmt = (
            select(Voucher)
            .join(Partner, Partner.id == Voucher.partner_id)
            .where(
                Voucher.business_id == business_id,
                Voucher.kind == kind,
                Voucher.voucher_date >= f.start,
                Voucher.voucher_date <= f.end,
            )
            .options(
                selectinload(Voucher.partner),
                selectinload(Voucher.lines).selectinload(VoucherLine.item),
            )
            .order_by(Voucher.voucher_date.desc(), Voucher.number.desc())
        )
        if f.q:
            stmt = stmt.where(or_(Partner.name.contains(f.q), Voucher.number.contains(f.q)))
        if f.status:
            stmt = stmt.where(Voucher.status == f.status)
        return stmt

    @router.get("")
    def index(request: Request, db: DB, _user: MenuUser, f: Annotated[VoucherFilter, Depends()]):
        business_id = request.state.business.id
        page = paginate(db, query(business_id, f), f.page)
        totals = {"supply": Decimal(0), "vat": Decimal(0), "total": Decimal(0)}
        for (v,) in db.execute(query(business_id, f)).all():
            if v.status in EFFECTIVE_STATUSES:
                totals["supply"] += v.supply_amount
                totals["vat"] += v.vat_amount
                totals["total"] += v.total_amount
        c = ctx(page=page, f=f, totals=totals, statuses=VOUCHER_STATUS_LABELS)
        return render(
            request, pick_template(request, "vouchers/list.html", "vouchers/_table.html"), c
        )

    @router.get("/export.xlsx")
    def export(request: Request, db: DB, _user: MenuUser, f: Annotated[VoucherFilter, Depends()]):
        rows = db.scalars(query(request.state.business.id, f))
        columns = [
            ("일자", lambda v: v.voucher_date),
            ("전표번호", lambda v: v.number),
            ("거래처", lambda v: v.partner.name),
            ("사업자번호", lambda v: v.partner.biz_no_display),
            ("품목", lambda v: _summary(v)),
            ("공급가액", lambda v: v.supply_amount),
            ("부가세", lambda v: v.vat_amount),
            ("합계", lambda v: v.total_amount),
            ("상태", lambda v: v.status_label),
            ("메모", lambda v: v.memo),
        ]
        return xlsx_response(f"{label}전표", columns, rows)

    # ---------- 조회용 JSON(전표 입력 화면) ----------

    @router.get("/lookup/partners")
    def lookup_partners(request: Request, db: DB, _user: MenuUser, q: str = ""):
        stmt = (
            select(Partner)
            .where(
                Partner.business_id == request.state.business.id,
                Partner.is_active,
                Partner.kind.in_(partner_kinds),
            )
            .order_by(Partner.name)
            .limit(LOOKUP_LIMIT)
        )
        if q.strip():
            stmt = stmt.where(
                or_(Partner.name.contains(q.strip()), Partner.biz_no.contains(q.strip()))
            )
        return [
            {"id": p.id, "name": p.name, "biz_no": p.biz_no_display, "status": p.biz_status}
            for p in db.scalars(stmt)
        ]

    @router.get("/lookup/items")
    def lookup_items(request: Request, db: DB, _user: MenuUser, q: str = ""):
        business_id = request.state.business.id
        stmt = (
            select(Item)
            .where(Item.business_id == business_id, Item.is_active)
            .options(
                selectinload(Item.stock_unit),
                selectinload(Item.content_unit),
                selectinload(Item.units).selectinload(ItemUnit.unit),
            )
            .order_by(Item.code_key)
            .limit(LOOKUP_LIMIT)
        )
        if q.strip():
            stmt = stmt.where(
                or_(Item.name.contains(q.strip()), Item.code_key.contains(master_svc.code_key(q)))
            )
        return _with_stock(db, business_id, list(db.scalars(stmt)), kind)

    # ---------- 등록·수정 ----------

    def form_page(request, db, voucher=None, form=None, error=None, status=200):
        business = request.state.business
        warehouses = list(
            db.scalars(
                select(Warehouse)
                .where(Warehouse.business_id == business.id, Warehouse.is_active)
                .order_by(Warehouse.is_default.desc(), Warehouse.name)
            )
        )
        initial = _initial(db, business.id, kind, voucher, form)
        c = ctx(
            voucher=voucher,
            error=error,
            warehouses=warehouses,
            initial=initial,
            charges_vat=business.charges_vat,
            locked=voucher is not None and closing.is_closed(db, business.id, voucher.voucher_date),
            links=_links(db, voucher),
        )
        return render(request, "vouchers/form.html", c, status_code=status)

    @router.get("/new")
    def new(request: Request, db: DB, _user: MenuUser):
        return form_page(request, db)

    def _save(request, db, user, voucher, form: VoucherForm):
        business = request.state.business
        try:
            lines = _parse_lines(form)
            if not form.partner_id.isdigit():
                raise VoucherError("거래처를 골라 주세요")
            saved = save_voucher(
                db, business, user, kind, voucher, form.voucher_date, int(form.partner_id),
                lines, form.memo,
            )  # fmt: skip
        except (VoucherError, closing.ClosedPeriodError) as exc:
            db.rollback()
            if voucher is not None:
                db.refresh(voucher)
            return form_page(request, db, voucher, form, str(exc), 400)
        db.commit()
        return saved

    @router.post("")
    def create(request: Request, db: DB, user: MenuUser, form: Annotated[VoucherForm, Form()]):
        result = _save(request, db, user, None, form)
        if not isinstance(result, Voucher):
            return result
        flash(request, f"{label} 전표를 저장했어요 · {result.number}")
        return RedirectResponse(f"{prefix}/{result.id}", status_code=303)

    @router.get("/{voucher_id}")
    def detail(request: Request, db: DB, _user: MenuUser, voucher_id: int):
        voucher = owned(db, voucher_id, request.state.business.id)
        if voucher is None:
            return RedirectResponse(prefix, status_code=303)
        editable = voucher.status == VoucherStatus.CONFIRMED and not voucher.reversal_of_id
        if editable and not closing.is_closed(db, voucher.business_id, voucher.voucher_date):
            return form_page(request, db, voucher)
        c = ctx(
            voucher=voucher,
            locked=closing.is_closed(db, voucher.business_id, voucher.voucher_date),
            reversal=db.scalar(select(Voucher).where(Voucher.reversal_of_id == voucher.id)),
            fmt_qty=master_svc.format_qty,
            links=_links(db, voucher),
        )
        return render(request, "vouchers/view.html", c)

    @router.post("/{voucher_id}")
    def update(
        request: Request,
        db: DB,
        user: MenuUser,
        voucher_id: int,
        form: Annotated[VoucherForm, Form()],
    ):
        voucher = owned(db, voucher_id, request.state.business.id)
        if voucher is None:
            return RedirectResponse(prefix, status_code=303)
        result = _save(request, db, user, voucher, form)
        if not isinstance(result, Voucher):
            return result
        flash(request, "저장했어요")
        return RedirectResponse(f"{prefix}/{result.id}", status_code=303)

    @router.post("/{voucher_id}/cancel")
    def cancel(request: Request, db: DB, user: MenuUser, voucher_id: int):
        voucher = owned(db, voucher_id, request.state.business.id)
        if voucher is None:
            return RedirectResponse(prefix, status_code=303)
        try:
            reversal = cancel_voucher(db, request.state.business, user, voucher)
        except (VoucherError, closing.ClosedPeriodError) as exc:
            db.rollback()
            flash(request, str(exc), "error")
            return RedirectResponse(f"{prefix}/{voucher_id}", status_code=303)
        db.commit()
        if reversal is not None:
            flash(request, f"마감된 달이라 취소 전표 {reversal.number}를 만들었어요")
            return RedirectResponse(f"{prefix}/{reversal.id}", status_code=303)
        flash(request, "전표를 취소했어요. 재고도 되돌렸어요")
        return RedirectResponse(f"{prefix}/{voucher_id}", status_code=303)

    @router.get("/{voucher_id}/print")
    def print_page(request: Request, db: DB, _user: MenuUser, voucher_id: int):
        voucher = owned(db, voucher_id, request.state.business.id)
        if voucher is None:
            return RedirectResponse(prefix, status_code=303)
        c = ctx(voucher=voucher, fmt_qty=master_svc.format_qty)
        return render(request, "vouchers/print.html", c)

    return router


def _links(db: Session, voucher: Voucher | None) -> dict | None:
    """전표에 연결된 수금·지급 금액과 세금계산서"""
    if voucher is None:
        return None
    paid = allocated_by_voucher(db, [voucher.id], EFFECTIVE_PAYMENT_STATUSES).get(voucher.id)
    return {
        "paid": paid or Decimal(0),
        "invoice": db.get(TaxInvoice, voucher.tax_invoice_id) if voucher.tax_invoice_id else None,
        "ledger": "/receivables" if voucher.kind == VoucherKind.SALES else "/payables",
        "paid_label": "수금" if voucher.kind == VoucherKind.SALES else "지급",
    }


def plain(value: Decimal) -> str:
    """Decimal을 지수 표기 없이(500.0000 → '500')"""
    return format(Decimal(value).normalize(), "f")


def _summary(v: Voucher) -> str:
    if not v.lines:
        return ""
    first = v.lines[0].item.name
    return first if len(v.lines) == 1 else f"{first} 외 {len(v.lines) - 1}건"


def _item_json(item: Item, kind: VoucherKind) -> dict:
    price = item.sale_price if kind == VoucherKind.SALES else item.purchase_price
    default_unit = item.sale_unit_id if kind == VoucherKind.SALES else item.purchase_unit_id
    units = [{"id": item.stock_unit_id, "name": item.stock_unit.name, "factor": "1"}] + [
        {"id": u.unit_id, "name": u.unit.name, "factor": plain(u.factor)} for u in item.units
    ]
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "spec": item.spec or "",
        "tax_exempt": item.tax_exempt,
        "price": plain(price),
        "stock_unit": item.stock_unit.name,
        "decimals": item.stock_unit.decimals,
        "units": units,
        # 품목에 정한 매출·매입 기본 거래 단위(없으면 재고 단위)
        "default_unit_id": default_unit or item.stock_unit_id,
    }


def _with_stock(db: Session, business_id: int, items: list[Item], kind: VoucherKind) -> list[dict]:
    totals = item_totals(db, business_id, [i.id for i in items])
    units = list(db.scalars(select(Unit).where(Unit.business_id == business_id)))
    out = []
    for item in items:
        data = _item_json(item, kind)
        data["stock"] = master_svc.stock_label(totals.get(item.id, Decimal(0)), item, units)
        out.append(data)
    return out


def _initial(db, business_id, kind, voucher: Voucher | None, form: VoucherForm | None) -> dict:
    """전표 입력 화면(Alpine)의 초기 상태"""
    if form is not None:
        partner = db.get(Partner, int(form.partner_id)) if form.partner_id.isdigit() else None
        ids = [int(i) for i in form.item_id if i.isdigit()]
        items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(ids)))}
        lines = []
        for idx, item_id in enumerate(form.item_id):
            if not item_id.isdigit() or int(item_id) not in items:
                continue
            item = items[int(item_id)]

            def at(values, i=idx):
                return values[i] if i < len(values) else ""

            lines.append(
                {
                    "item": _item_json(item, kind),
                    "unit_id": at(form.unit_id),
                    "qty": at(form.qty),
                    "unit_price": at(form.unit_price),
                    "warehouse_id": at(form.warehouse_id),
                    "memo": at(form.line_memo),
                }
            )
        return {
            "date": form.voucher_date.isoformat(),
            "partner": {"id": partner.id, "name": partner.name} if partner else None,
            "memo": form.memo,
            "lines": lines,
        }
    if voucher is not None:
        return {
            "date": voucher.voucher_date.isoformat(),
            "partner": {"id": voucher.partner.id, "name": voucher.partner.name},
            "memo": voucher.memo or "",
            "lines": [
                {
                    "item": _item_json(ln.item, kind),
                    "unit_id": str(ln.unit_id),
                    "qty": plain(ln.qty),
                    "unit_price": plain(ln.unit_price),
                    "warehouse_id": str(ln.warehouse_id),
                    "memo": ln.memo or "",
                }
                for ln in voucher.lines
            ],
        }
    return {"date": today().isoformat(), "partner": None, "memo": "", "lines": []}


sales_router = make_router(VoucherKind.SALES)
purchase_router = make_router(VoucherKind.PURCHASE)
