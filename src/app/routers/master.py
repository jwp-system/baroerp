from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_menu
from app.db import get_db
from app.export import xlsx_response
from app.listing import paginate, pick_template
from app.models import Account, Item, ItemGroup, ItemUnit, Partner, Unit, User, Warehouse
from app.models.accounting import ACCOUNT_KIND_LABELS, AccountKind
from app.models.master import (
    BIZ_STATUS_LABELS,
    PARTNER_KIND_LABELS,
    UNIT_KIND_LABELS,
    BizStatus,
    PartnerKind,
    UnitKind,
)
from app.services import master as svc
from app.services.excel_import import ITEM_COLUMNS, PARTNER_COLUMNS, import_items, import_partners
from app.services.nts import biz_no_error, digits, fetch_statuses, refresh_partner_statuses
from app.services.voucher import item_totals
from app.templating import flash, render

router = APIRouter(prefix="/master", include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]
MasterUser = Annotated[User, require_menu("master")]

TABS = [
    ("partners", "거래처", "/master/partners"),
    ("items", "품목", "/master/items"),
    ("groups", "품목그룹", "/master/groups"),
    ("units", "단위", "/master/units"),
    ("trade_units", "거래 단위", "/master/trade-units"),
    ("warehouses", "창고", "/master/warehouses"),
    ("accounts", "계정과목", "/master/accounts"),
    ("import", "엑셀 일괄 등록", "/master/import"),
]


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _ctx(tab: str, **kw) -> dict:
    return {"active": "master", "tabs": TABS, "tab": tab, **kw}


def _decimal(value: str | None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise svc.MasterError(f"'{value}'은(는) 숫자가 아니에요") from exc


def _owned(db: Session, model, obj_id: int, business_id: int):
    obj = db.get(model, obj_id)
    if obj is None or obj.business_id != business_id:
        return None
    return obj


def _units(db: Session, business_id: int, active_only: bool = False) -> list[Unit]:
    stmt = select(Unit).where(Unit.business_id == business_id).order_by(Unit.sort_order, Unit.id)
    if active_only:
        stmt = stmt.where(Unit.is_active)
    return list(db.scalars(stmt))


@router.get("")
def index(_user: MasterUser):
    return _redirect("/master/partners")


# =====================================================================
# 거래처
# =====================================================================


class PartnerFilter:
    def __init__(
        self,
        q: Annotated[str, Query()] = "",
        kind: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        active: Annotated[str, Query()] = "1",
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        self.q, self.kind, self.status, self.active, self.page = (
            q.strip(),
            kind,
            status,
            active,
            page,
        )

    def params(self) -> dict:
        return {"q": self.q, "kind": self.kind, "status": self.status, "active": self.active}


def _partner_query(business_id: int, f: PartnerFilter):
    stmt = select(Partner).where(Partner.business_id == business_id).order_by(Partner.name)
    if f.q:
        stmt = stmt.where(
            or_(Partner.name.contains(f.q), Partner.biz_no.contains(digits(f.q) or f.q))
        )
    if f.kind:
        # 공통 거래처는 매출·매입 양쪽에 함께 보인다
        stmt = stmt.where(Partner.kind.in_([f.kind, PartnerKind.BOTH]))
    if f.status:
        stmt = stmt.where(Partner.biz_status == f.status)
    if f.active in ("0", "1"):
        stmt = stmt.where(Partner.is_active == (f.active == "1"))
    return stmt


@router.get("/partners")
def partners(request: Request, db: DB, _user: MasterUser, f: Annotated[PartnerFilter, Depends()]):
    page = paginate(db, _partner_query(request.state.business.id, f), f.page)
    ctx = _ctx(
        "partners",
        page=page,
        f=f,
        kinds=PARTNER_KIND_LABELS,
        statuses=BIZ_STATUS_LABELS,
    )
    template = pick_template(request, "master/partners.html", "master/_partner_table.html")
    return render(request, template, ctx)


def _partner_export_columns():
    getters = {
        "거래처명": lambda p: p.name,
        "구분": lambda p: p.kind_label,
        "사업자번호": lambda p: p.biz_no_display,
        "대표자": lambda p: p.ceo_name,
        "업태": lambda p: p.biz_type,
        "종목": lambda p: p.biz_item,
        "주소": lambda p: p.address,
        "전화": lambda p: p.phone,
        "이메일": lambda p: p.email,
        "담당자": lambda p: p.contact_name,
        "담당자 연락처": lambda p: p.contact_phone,
        "담당자 이메일": lambda p: p.contact_email,
        "결제조건(일)": lambda p: p.payment_terms_days,
        "여신한도": lambda p: p.credit_limit,
        "메모": lambda p: p.memo,
    }
    # 내보낸 파일을 고쳐 그대로 다시 올릴 수 있도록 일괄 등록 양식과 같은 열 순서
    return [(name, getters[name]) for name, _, _ in PARTNER_COLUMNS] + [
        ("사업자 상태", lambda p: p.biz_status_label),
        ("사용", lambda p: "사용" if p.is_active else "사용 안 함"),
    ]


@router.get("/partners/export.xlsx")
def partners_export(
    request: Request, db: DB, _user: MasterUser, f: Annotated[PartnerFilter, Depends()]
):
    rows = db.scalars(_partner_query(request.state.business.id, f))
    return xlsx_response("거래처", _partner_export_columns(), rows)


class PartnerForm(BaseModel):
    name: str
    kind: PartnerKind
    biz_no: str = ""
    ceo_name: str = ""
    biz_type: str = ""
    biz_item: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    contact_email: str = ""
    payment_terms_days: int = 30
    credit_limit: str = ""
    memo: str = ""
    is_active: bool = False


def _partner_form(request: Request, partner: Partner | None, form=None, error=None, status=200):
    ctx = _ctx("partners", partner=partner, form=form, error=error, kinds=PARTNER_KIND_LABELS)
    return render(request, "master/partner_form.html", ctx, status_code=status)


def _apply_partner(db: Session, business_id: int, partner: Partner, form: PartnerForm) -> bool:
    """폼 값을 반영하고, 사업자번호가 바뀌었으면 True."""
    biz_no = digits(form.biz_no) or None
    if biz_no and (msg := biz_no_error(biz_no)):
        raise svc.MasterError(msg)
    if biz_no:
        dup = db.scalar(
            select(Partner.name).where(
                Partner.business_id == business_id,
                Partner.biz_no == biz_no,
                Partner.id != (partner.id or 0),
            )
        )
        if dup:
            raise svc.MasterError(f"같은 사업자번호로 '{dup}' 거래처가 이미 있어요")
    changed = biz_no != partner.biz_no
    partner.name = form.name.strip()
    partner.kind = form.kind
    partner.biz_no = biz_no
    for key in (
        "ceo_name", "biz_type", "biz_item", "address", "phone", "email",
        "contact_name", "contact_phone", "contact_email", "memo",
    ):  # fmt: skip
        setattr(partner, key, getattr(form, key).strip() or None)
    partner.payment_terms_days = form.payment_terms_days
    partner.credit_limit = _decimal(form.credit_limit)
    if changed:
        partner.biz_status = BizStatus.UNKNOWN
        partner.biz_status_checked_at = None
    return changed


@router.get("/partners/new")
def partner_new(request: Request, _user: MasterUser):
    return _partner_form(request, None)


@router.post("/partners")
def partner_create(
    request: Request,
    db: DB,
    _user: MasterUser,
    form: Annotated[PartnerForm, Form()],
    tasks: BackgroundTasks,
):
    business_id = request.state.business.id
    partner = Partner(business_id=business_id, is_active=True)
    try:
        _apply_partner(db, business_id, partner, form)
    except svc.MasterError as exc:
        return _partner_form(request, None, form, str(exc), 400)
    db.add(partner)
    db.commit()
    if partner.biz_no:
        tasks.add_task(refresh_partner_statuses, business_id, [partner.id])
    flash(request, f"'{partner.name}' 거래처를 등록했어요")
    return _redirect("/master/partners")


@router.get("/partners/{partner_id}")
def partner_edit(request: Request, db: DB, _user: MasterUser, partner_id: int):
    partner = _owned(db, Partner, partner_id, request.state.business.id)
    if partner is None:
        return _redirect("/master/partners")
    return _partner_form(request, partner)


@router.post("/partners/{partner_id}")
def partner_update(
    request: Request,
    db: DB,
    _user: MasterUser,
    partner_id: int,
    form: Annotated[PartnerForm, Form()],
    tasks: BackgroundTasks,
):
    business_id = request.state.business.id
    partner = _owned(db, Partner, partner_id, business_id)
    if partner is None:
        return _redirect("/master/partners")
    try:
        changed = _apply_partner(db, business_id, partner, form)
    except svc.MasterError as exc:
        db.rollback()
        return _partner_form(request, partner, form, str(exc), 400)
    partner.is_active = form.is_active
    db.commit()
    if changed and partner.biz_no:
        tasks.add_task(refresh_partner_statuses, business_id, [partner.id])
    flash(request, "저장했어요")
    return _redirect("/master/partners")


@router.post("/partners/{partner_id}/check")
def partner_check(request: Request, db: DB, _user: MasterUser, partner_id: int):
    partner = _owned(db, Partner, partner_id, request.state.business.id)
    if partner is None or not partner.biz_no:
        return _redirect("/master/partners")
    statuses = fetch_statuses([partner.biz_no])
    if partner.biz_no in statuses:
        partner.biz_status = statuses[partner.biz_no]
        partner.biz_status_checked_at = datetime.now(UTC)
        db.commit()
        flash(request, f"국세청 조회 결과: {partner.biz_status_label}")
    else:
        flash(
            request,
            "지금은 조회할 수 없어요. 서비스키 설정이나 잠시 후 다시 시도해 주세요",
            "error",
        )
    return _redirect(f"/master/partners/{partner.id}")


# =====================================================================
# 품목
# =====================================================================


class ItemFilter:
    def __init__(
        self,
        q: Annotated[str, Query()] = "",
        group: Annotated[str, Query()] = "",
        active: Annotated[str, Query()] = "1",
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        self.q, self.active, self.page = q.strip(), active, page
        self.group = int(group) if group.isdigit() else None

    def params(self) -> dict:
        return {"q": self.q, "group": self.group or "", "active": self.active}


def _item_query(db: Session, business_id: int, f: ItemFilter):
    stmt = (
        select(Item)
        .where(Item.business_id == business_id)
        .options(
            selectinload(Item.group).selectinload(ItemGroup.parent),
            selectinload(Item.stock_unit),
            selectinload(Item.content_unit),
            selectinload(Item.units).selectinload(ItemUnit.unit),
        )
        .order_by(Item.code_key)
    )
    if f.q:
        stmt = stmt.where(or_(Item.name.contains(f.q), Item.code_key.contains(svc.code_key(f.q))))
    if f.group:
        group = _owned(db, ItemGroup, f.group, business_id)
        if group is not None:
            # 상위 그룹을 고르면 하위 그룹 품목까지
            stmt = stmt.where(Item.group_id.in_(svc.descendant_ids(db, group)))
    if f.active in ("0", "1"):
        stmt = stmt.where(Item.is_active == (f.active == "1"))
    return stmt


@router.get("/items")
def items(request: Request, db: DB, _user: MasterUser, f: Annotated[ItemFilter, Depends()]):
    business_id = request.state.business.id
    page = paginate(db, _item_query(db, business_id, f), f.page)
    ctx = _ctx(
        "items",
        page=page,
        stock=item_totals(db, business_id, [i.id for (i,) in page.rows]),
        stock_label=svc.stock_label,
        f=f,
        groups=svc.group_options(db, business_id),
        units=_units(db, business_id),
        content=svc.format_content,
        fmt_qty=svc.format_qty,
        path=svc.path_label,
    )
    return render(
        request, pick_template(request, "master/items.html", "master/_item_table.html"), ctx
    )


@router.get("/items/export.xlsx")
def items_export(request: Request, db: DB, _user: MasterUser, f: Annotated[ItemFilter, Depends()]):
    rows = db.scalars(_item_query(db, request.state.business.id, f))

    def trade(i, attr):
        u = i.units[0] if i.units else None
        if u is None:
            return None
        return u.unit.name if attr == "unit" else u.factor

    def default_unit(i, unit_id):
        if unit_id is None:
            return None
        return next((u.unit.name for u in i.units if u.unit_id == unit_id), None)

    getters = {
        "식별코드": lambda i: i.code,
        "품목명": lambda i: i.name,
        "품목그룹": lambda i: svc.path_label(i.group),
        "규격": lambda i: i.spec,
        "재고단위": lambda i: i.stock_unit.name,
        "단위용량": lambda i: i.content_qty,
        "용량단위": lambda i: i.content_unit.name if i.content_unit else None,
        "거래단위": lambda i: trade(i, "unit"),
        "환산수량": lambda i: trade(i, "factor"),
        "매입기본단위": lambda i: default_unit(i, i.purchase_unit_id),
        "매출기본단위": lambda i: default_unit(i, i.sale_unit_id),
        "매입단가": lambda i: i.purchase_price,
        "판매단가": lambda i: i.sale_price,
        "과세구분": lambda i: "면세" if i.tax_exempt else "과세",
        "적정재고": lambda i: i.safety_stock,
    }
    columns = [(name, getters[name]) for name, _, _ in ITEM_COLUMNS] + [
        ("사용", lambda i: "사용" if i.is_active else "사용 안 함"),
    ]
    return xlsx_response("품목", columns, rows)


class ItemForm(BaseModel):
    code: str = ""
    name: str
    group_id: int
    spec: str = ""
    stock_unit_id: int
    content_qty: str = ""
    content_unit_id: str = ""
    purchase_price: str = "0"
    sale_price: str = "0"
    tax_exempt: bool = False
    safety_stock: str = ""
    is_active: bool = False
    trade_unit_id: list[str] = []
    trade_factor: list[str] = []
    purchase_unit_id: str = ""
    sale_unit_id: str = ""


def _item_form(request: Request, db: Session, item: Item | None, form=None, error=None, status=200):
    business_id = request.state.business.id
    groups = svc.group_options(db, business_id, leaf_only=True)
    if item is not None and all(g.id != item.group_id for g, _ in groups):
        groups.append((item.group, 0))
    if form is not None:
        trades = [
            {"unit": u, "factor": f}
            for u, f in zip(form.trade_unit_id, form.trade_factor, strict=False)
        ]
    elif item is not None:
        trades = [
            {"unit": str(u.unit_id), "factor": svc.format_qty(u.factor, 4)} for u in item.units
        ]
    else:
        trades = []
    ctx = _ctx(
        "items",
        item=item,
        form=form,
        trades=trades,
        error=error,
        groups=groups,
        path=svc.path_label,
        units=_units(db, business_id, active_only=True),
        unit_names={u.id: u.name for u in _units(db, business_id)},
        group_defaults={
            g.id: {"unit": g.default_unit_id, "tax_exempt": g.default_tax_exempt} for g, _ in groups
        },
    )
    return render(request, "master/item_form.html", ctx, status_code=status)


def _apply_item(db: Session, business, item: Item, form: ItemForm) -> None:
    group = _owned(db, ItemGroup, form.group_id, business.id)
    if group is None:
        raise svc.MasterError("품목그룹을 골라 주세요")
    if group.id != item.group_id:
        svc.ensure_leaf(db, group)
    stock_unit = _owned(db, Unit, form.stock_unit_id, business.id)
    if stock_unit is None:
        raise svc.MasterError("재고 단위를 골라 주세요")

    code = form.code.strip()
    if code:
        svc.ensure_unique_code(db, Item, business.id, code, exclude_id=item.id)
    elif item.id is None:
        code = svc.next_item_code(db, business, group)
    if code:
        item.code, item.code_key = code, svc.code_key(code)

    content_qty = _decimal(form.content_qty)
    content_unit = (
        _owned(db, Unit, int(form.content_unit_id), business.id)
        if form.content_unit_id.isdigit()
        else None
    )
    if (content_qty is None) != (content_unit is None):
        raise svc.MasterError("단위 용량은 수량과 단위를 함께 입력해 주세요")
    if content_unit is not None and content_unit.kind == UnitKind.COUNT:
        raise svc.MasterError("용량 단위는 부피·무게·길이 단위 중에서 골라 주세요")

    item.name = form.name.strip()
    item.group_id = group.id
    item.spec = form.spec.strip() or None
    item.stock_unit_id = stock_unit.id
    item.content_qty = content_qty
    item.content_unit_id = content_unit.id if content_unit else None
    item.purchase_price = _decimal(form.purchase_price) or Decimal(0)
    item.sale_price = _decimal(form.sale_price) or Decimal(0)
    item.tax_exempt = form.tax_exempt
    item.safety_stock = _decimal(form.safety_stock)

    # 거래 단위 환산
    wanted: dict[int, Decimal] = {}
    for unit_id, factor in zip(form.trade_unit_id, form.trade_factor, strict=False):
        if not unit_id.isdigit() or not factor.strip():
            continue
        unit = _owned(db, Unit, int(unit_id), business.id)
        f = _decimal(factor)
        if unit is None or unit.id == stock_unit.id:
            continue
        if f is None or f <= 0:
            raise svc.MasterError("거래 단위 환산 수량은 0보다 커야 해요")
        wanted[unit.id] = f
    for existing in list(item.units):
        if existing.unit_id not in wanted:
            item.units.remove(existing)
        else:
            existing.factor = wanted.pop(existing.unit_id)
    for unit_id, f in wanted.items():
        item.units.append(ItemUnit(business_id=business.id, unit_id=unit_id, factor=f))

    # 매입·매출 기본 거래 단위: 재고 단위(비움)나 위의 거래 단위 중 하나
    allowed = {stock_unit.id} | {u.unit_id for u in item.units}
    for attr, label in (("purchase_unit_id", "매입"), ("sale_unit_id", "매출")):
        value = getattr(form, attr)
        unit_id = int(value) if value.isdigit() else None
        if unit_id is not None and unit_id not in allowed:
            raise svc.MasterError(f"{label} 기본 단위는 재고 단위나 거래 단위 중에서 골라 주세요")
        setattr(item, attr, None if unit_id == stock_unit.id else unit_id)


@router.get("/items/new")
def item_new(request: Request, db: DB, _user: MasterUser):
    return _item_form(request, db, None)


@router.get("/items/code-preview", response_class=HTMLResponse)
def item_code_preview(request: Request, db: DB, _user: MasterUser, group_id: int = 0):
    group = _owned(db, ItemGroup, group_id, request.state.business.id)
    if group is None:
        return ""
    return f"비우면 자동으로 <b>{svc.next_item_code(db, request.state.business, group)}</b>"


@router.post("/items")
def item_create(request: Request, db: DB, _user: MasterUser, form: Annotated[ItemForm, Form()]):
    business = request.state.business
    item = Item(business_id=business.id, is_active=True)
    try:
        _apply_item(db, business, item, form)
    except svc.MasterError as exc:
        db.rollback()
        return _item_form(request, db, None, form, str(exc), 400)
    db.add(item)
    db.commit()
    flash(request, f"'{item.name}' 품목을 등록했어요 · {item.code}")
    return _redirect("/master/items")


@router.get("/items/{item_id}")
def item_edit(request: Request, db: DB, _user: MasterUser, item_id: int):
    item = _owned(db, Item, item_id, request.state.business.id)
    if item is None:
        return _redirect("/master/items")
    return _item_form(request, db, item)


@router.post("/items/{item_id}")
def item_update(
    request: Request, db: DB, _user: MasterUser, item_id: int, form: Annotated[ItemForm, Form()]
):
    business = request.state.business
    item = _owned(db, Item, item_id, business.id)
    if item is None:
        return _redirect("/master/items")
    try:
        _apply_item(db, business, item, form)
    except svc.MasterError as exc:
        db.rollback()
        return _item_form(request, db, item, form, str(exc), 400)
    item.is_active = form.is_active
    db.commit()
    flash(request, "저장했어요")
    return _redirect("/master/items")


# =====================================================================
# 거래 단위: 품목별 환산을 한 화면에서 보고 여러 품목에 한 번에 적용
# =====================================================================


class TradeUnitFilter(ItemFilter):
    def __init__(
        self,
        q: Annotated[str, Query()] = "",
        group: Annotated[str, Query()] = "",
        active: Annotated[str, Query()] = "1",
        unit: Annotated[str, Query()] = "",
        has: Annotated[str, Query()] = "",
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        super().__init__(q, group, active, page)
        self.unit = int(unit) if unit.isdigit() else None
        self.has = has if has in ("with", "without") else ""

    def params(self) -> dict:
        return {**super().params(), "unit": self.unit or "", "has": self.has}


def _trade_unit_query(db: Session, business_id: int, f: TradeUnitFilter):
    stmt = _item_query(db, business_id, f)
    if f.unit:
        stmt = stmt.where(Item.units.any(ItemUnit.unit_id == f.unit))
    if f.has == "with":
        stmt = stmt.where(Item.units.any())
    elif f.has == "without":
        stmt = stmt.where(~Item.units.any())
    return stmt


@router.get("/trade-units")
def trade_units(
    request: Request, db: DB, _user: MasterUser, f: Annotated[TradeUnitFilter, Depends()]
):
    business_id = request.state.business.id
    page = paginate(db, _trade_unit_query(db, business_id, f), f.page)
    ctx = _ctx(
        "trade_units",
        page=page,
        f=f,
        groups=svc.group_options(db, business_id),
        units=_units(db, business_id, active_only=True),
        fmt_qty=svc.format_qty,
        path=svc.path_label,
    )
    return render(
        request,
        pick_template(request, "master/trade_units.html", "master/_trade_unit_table.html"),
        ctx,
    )


class TradeUnitForm(BaseModel):
    action: str = "apply"  # apply | remove
    scope: str = "selected"  # selected | filtered
    item_id: list[int] = []
    unit_id: int
    factor: str = ""
    purchase_default: bool = False
    sale_default: bool = False
    # 검색 조건(scope=filtered일 때 대상, 처리 후 같은 목록으로 돌아가기)
    q: str = ""
    group: str = ""
    active: str = "1"
    unit: str = ""
    has: str = ""


@router.post("/trade-units")
def trade_units_apply(
    request: Request, db: DB, _user: MasterUser, form: Annotated[TradeUnitForm, Form()]
):
    business = request.state.business
    f = TradeUnitFilter(form.q, form.group, form.active, form.unit, form.has)
    back = "/master/trade-units?" + urlencode(f.params())
    unit = _owned(db, Unit, form.unit_id, business.id)
    if unit is None:
        flash(request, "거래 단위를 골라 주세요", "error")
        return _redirect(back)
    if form.scope == "filtered":
        items = list(db.scalars(_trade_unit_query(db, business.id, f)))
    else:
        ids = set(form.item_id)
        items = list(
            db.scalars(
                select(Item)
                .where(Item.business_id == business.id, Item.id.in_(ids))
                .options(selectinload(Item.units))
            )
        )
    if not items:
        flash(request, "적용할 품목을 골라 주세요", "error")
        return _redirect(back)
    try:
        if form.action == "remove":
            removed = svc.remove_trade_unit(items, unit)
            message = f"{removed}개 품목에서 '{unit.name}' 환산을 지웠어요"
        else:
            factor = _decimal(form.factor)
            if factor is None:
                raise svc.MasterError("환산 수량을 입력해 주세요")
            applied, skipped = svc.apply_trade_unit(
                business, items, unit, factor, form.purchase_default, form.sale_default
            )
            qty = svc.format_qty(factor, 4)
            message = f"{applied}개 품목에 '{unit.name}' 환산(재고 단위 {qty})을 적용했어요"
            if skipped:
                message += f" · 재고 단위가 {unit.name}인 {skipped}개는 건너뛰었어요"
    except svc.MasterError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(back)
    db.commit()
    flash(request, message)
    return _redirect(back)


# =====================================================================
# 품목그룹
# =====================================================================


@router.get("/groups")
def groups(request: Request, db: DB, _user: MasterUser):
    business_id = request.state.business.id
    counts = dict(
        db.execute(
            select(Item.group_id, func.count())
            .where(Item.business_id == business_id)
            .group_by(Item.group_id)
        ).all()
    )
    tree = svc.group_options(db, business_id)
    parents = {g.parent_id for g, _ in tree}
    ctx = _ctx(
        "groups",
        tree=tree,
        counts=counts,
        parents=parents,
        parent_options=[
            (g, d)
            for g, d in tree
            if not g.is_uncategorized and g.level < request.state.business.item_group_depth
        ],
    )
    return render(request, "master/groups.html", ctx)


@router.post("/groups/settings")
def groups_settings(
    request: Request,
    db: DB,
    _user: MasterUser,
    item_group_depth: Annotated[int, Form(ge=1, le=3)],
    item_code_format: Annotated[str, Form()],
):
    business = request.state.business
    deepest = (
        db.scalar(select(func.max(ItemGroup.level)).where(ItemGroup.business_id == business.id))
        or 1
    )
    if item_group_depth < deepest:
        flash(
            request,
            f"이미 {deepest}단계 그룹이 있어 {item_group_depth}단계로 줄일 수 없어요",
            "error",
        )
        return _redirect("/master/groups")
    business.item_group_depth = item_group_depth
    business.item_code_format = item_code_format.strip() or "{GROUP}-{SEQ:4}"
    db.commit()
    flash(request, "품목그룹 설정을 저장했어요")
    return _redirect("/master/groups")


class GroupForm(BaseModel):
    name: str
    code: str
    parent_id: str = ""
    sort_order: int = 0
    default_unit_id: str = ""
    default_tax_exempt: bool = False
    default_safety_stock: str = ""
    code_format: str = ""
    is_active: bool = False  # 체크 해제 시 값이 오지 않는다(생성 시에는 무시)


def _apply_group(db: Session, business, group: ItemGroup, form: GroupForm) -> None:
    svc.ensure_unique_code(db, ItemGroup, business.id, form.code, exclude_id=group.id)
    parent = (
        _owned(db, ItemGroup, int(form.parent_id), business.id)
        if form.parent_id.isdigit()
        else None
    )
    if group.is_uncategorized and parent is not None:
        raise svc.MasterError("미분류 그룹은 다른 그룹 아래로 옮길 수 없어요")
    if group.id is None or parent_id_changed(group, parent):
        level = svc.validate_parent(db, business, group if group.id else None, parent)
        group.parent_id = parent.id if parent else None
        group.parent = parent
        if group.id is None:
            group.level = level
        else:
            svc.relevel(db, group, level)
    group.name = form.name.strip()
    group.code, group.code_key = form.code.strip(), svc.code_key(form.code)
    group.sort_order = form.sort_order
    group.default_unit_id = int(form.default_unit_id) if form.default_unit_id.isdigit() else None
    group.default_tax_exempt = form.default_tax_exempt
    group.default_safety_stock = _decimal(form.default_safety_stock)
    group.code_format = form.code_format.strip() or None


def parent_id_changed(group: ItemGroup, parent: ItemGroup | None) -> bool:
    return group.parent_id != (parent.id if parent else None)


@router.post("/groups")
def group_create(request: Request, db: DB, _user: MasterUser, form: Annotated[GroupForm, Form()]):
    business = request.state.business
    group = ItemGroup(business_id=business.id)
    try:
        _apply_group(db, business, group, form)
    except svc.MasterError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect("/master/groups")
    db.add(group)
    db.commit()
    flash(request, f"'{group.name}' 그룹을 만들었어요")
    return _redirect("/master/groups")


@router.get("/groups/{group_id}")
def group_edit(request: Request, db: DB, _user: MasterUser, group_id: int, error: str = ""):
    business = request.state.business
    group = _owned(db, ItemGroup, group_id, business.id)
    if group is None:
        return _redirect("/master/groups")
    excluded = svc.descendant_ids(db, group)
    ctx = _ctx(
        "groups",
        group=group,
        parent_options=[
            (g, d)
            for g, d in svc.group_options(db, business.id)
            if g.id not in excluded
            and not g.is_uncategorized
            and g.level < business.item_group_depth
        ],
        units=_units(db, business.id, active_only=True),
        item_count=db.scalar(select(func.count()).where(Item.group_id.in_(excluded))),
        has_children=svc.has_children(db, group),
        format_hint=svc.effective_code_format(business, group),
    )
    return render(request, "master/group_form.html", ctx)


@router.post("/groups/{group_id}")
def group_update(
    request: Request, db: DB, _user: MasterUser, group_id: int, form: Annotated[GroupForm, Form()]
):
    business = request.state.business
    group = _owned(db, ItemGroup, group_id, business.id)
    if group is None:
        return _redirect("/master/groups")
    try:
        _apply_group(db, business, group, form)
        if group.is_uncategorized and not form.is_active:
            raise svc.MasterError("미분류 그룹은 사용 안 함으로 바꿀 수 없어요")
    except svc.MasterError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/master/groups/{group_id}")
    group.is_active = form.is_active
    db.commit()
    flash(request, "저장했어요")
    return _redirect("/master/groups")


@router.post("/groups/{group_id}/delete")
def group_delete(request: Request, db: DB, _user: MasterUser, group_id: int):
    group = _owned(db, ItemGroup, group_id, request.state.business.id)
    if group is None:
        return _redirect("/master/groups")
    if group.is_uncategorized:
        flash(request, "미분류 그룹은 지울 수 없어요", "error")
    elif svc.has_children(db, group) or svc.has_items(db, group):
        # 품목·하위 그룹이 있으면 삭제 대신 사용 안 함
        group.is_active = False
        db.commit()
        flash(request, f"'{group.name}'에 품목이나 하위 그룹이 있어 '사용 안 함'으로 바꿨어요")
    else:
        db.delete(group)
        db.commit()
        flash(request, f"'{group.name}' 그룹을 지웠어요")
    return _redirect("/master/groups")


@router.post("/groups/{group_id}/adjust-price")
def group_adjust_price(
    request: Request,
    db: DB,
    _user: MasterUser,
    group_id: int,
    target: Annotated[str, Form(pattern="^(sale|purchase)$")],
    percent: Annotated[Decimal, Form(ge=-90, le=1000)],
):
    group = _owned(db, ItemGroup, group_id, request.state.business.id)
    if group is None:
        return _redirect("/master/groups")
    field = "sale_price" if target == "sale" else "purchase_price"
    ratio = 1 + percent / 100
    targets = db.scalars(select(Item).where(Item.group_id.in_(svc.descendant_ids(db, group))))
    count = 0
    for item in targets:
        setattr(item, field, (getattr(item, field) * ratio).quantize(Decimal(1)))
        count += 1
    db.commit()
    label = "판매단가" if target == "sale" else "매입단가"
    flash(request, f"품목 {count}개의 {label}를 {percent:+}% 조정했어요")
    return _redirect(f"/master/groups/{group_id}")


# =====================================================================
# 단위
# =====================================================================


@router.get("/units")
def units(request: Request, db: DB, _user: MasterUser):
    business_id = request.state.business.id
    used = set(db.scalars(select(Item.stock_unit_id).where(Item.business_id == business_id)))
    used |= set(db.scalars(select(Item.content_unit_id).where(Item.business_id == business_id)))
    used |= set(db.scalars(select(ItemUnit.unit_id).where(ItemUnit.business_id == business_id)))
    ctx = _ctx("units", units=_units(db, business_id), kinds=UNIT_KIND_LABELS, used=used)
    return render(request, "master/units.html", ctx)


@router.post("/units")
def unit_create(
    request: Request,
    db: DB,
    _user: MasterUser,
    name: Annotated[str, Form(min_length=1, max_length=20)],
    symbol: Annotated[str, Form(min_length=1, max_length=10)],
    kind: Annotated[UnitKind, Form()],
    base_ratio: Annotated[Decimal, Form(gt=0)] = Decimal(1),
    decimals: Annotated[int, Form(ge=0, le=4)] = 0,
):
    business_id = request.state.business.id
    if db.scalar(select(Unit.id).where(Unit.business_id == business_id, Unit.name == name.strip())):
        flash(request, f"'{name}' 단위가 이미 있어요", "error")
        return _redirect("/master/units")
    db.add(
        Unit(
            business_id=business_id,
            name=name.strip(),
            symbol=symbol.strip(),
            kind=kind,
            base_ratio=Decimal(1) if kind == UnitKind.COUNT else base_ratio,
            decimals=decimals,
            sort_order=100,
        )
    )
    db.commit()
    flash(request, f"'{name}' 단위를 추가했어요")
    return _redirect("/master/units")


@router.post("/units/{unit_id}")
def unit_update(
    request: Request,
    db: DB,
    _user: MasterUser,
    unit_id: int,
    symbol: Annotated[str, Form(min_length=1, max_length=10)],
    decimals: Annotated[int, Form(ge=0, le=4)],
    is_active: Annotated[bool, Form()] = False,
):
    unit = _owned(db, Unit, unit_id, request.state.business.id)
    if unit is None:
        return _redirect("/master/units")
    unit.symbol = symbol.strip()
    unit.decimals = decimals
    unit.is_active = is_active
    db.commit()
    flash(request, "저장했어요")
    return _redirect("/master/units")


# =====================================================================
# 창고
# =====================================================================


@router.get("/warehouses")
def warehouses(request: Request, db: DB, _user: MasterUser):
    rows = db.scalars(
        select(Warehouse)
        .where(Warehouse.business_id == request.state.business.id)
        .order_by(Warehouse.is_default.desc(), Warehouse.name)
    ).all()
    return render(request, "master/warehouses.html", _ctx("warehouses", warehouses=rows))


def _set_default_warehouse(db: Session, business_id: int, warehouse: Warehouse) -> None:
    for w in db.scalars(select(Warehouse).where(Warehouse.business_id == business_id)):
        w.is_default = w.id == warehouse.id
    warehouse.is_default = True


@router.post("/warehouses")
def warehouse_create(
    request: Request,
    db: DB,
    _user: MasterUser,
    name: Annotated[str, Form(min_length=1, max_length=50)],
    address: Annotated[str, Form()] = "",
):
    business_id = request.state.business.id
    if db.scalar(
        select(Warehouse.id).where(
            Warehouse.business_id == business_id, Warehouse.name == name.strip()
        )
    ):
        flash(request, f"'{name}' 창고가 이미 있어요", "error")
        return _redirect("/master/warehouses")
    db.add(Warehouse(business_id=business_id, name=name.strip(), address=address.strip() or None))
    db.commit()
    flash(request, f"'{name}' 창고를 추가했어요")
    return _redirect("/master/warehouses")


@router.post("/warehouses/{warehouse_id}")
def warehouse_update(
    request: Request,
    db: DB,
    _user: MasterUser,
    warehouse_id: int,
    name: Annotated[str, Form(min_length=1, max_length=50)],
    address: Annotated[str, Form()] = "",
    is_active: Annotated[bool, Form()] = False,
    is_default: Annotated[bool, Form()] = False,
):
    business_id = request.state.business.id
    warehouse = _owned(db, Warehouse, warehouse_id, business_id)
    if warehouse is None:
        return _redirect("/master/warehouses")
    if warehouse.is_default and not is_active:
        flash(
            request,
            "기본 창고는 사용 안 함으로 바꿀 수 없어요. 다른 창고를 기본으로 먼저 정해 주세요",
            "error",
        )
        return _redirect("/master/warehouses")
    warehouse.name = name.strip()
    warehouse.address = address.strip() or None
    warehouse.is_active = is_active
    if is_default and is_active:
        _set_default_warehouse(db, business_id, warehouse)
    db.commit()
    flash(request, "저장했어요")
    return _redirect("/master/warehouses")


# =====================================================================
# 계정과목
# =====================================================================


@router.get("/accounts")
def accounts(request: Request, db: DB, _user: MasterUser):
    rows = db.scalars(
        select(Account)
        .where(Account.business_id == request.state.business.id)
        .order_by(Account.code)
    ).all()
    ctx = _ctx("accounts", accounts=rows, kinds=ACCOUNT_KIND_LABELS)
    return render(request, "master/accounts.html", ctx)


@router.post("/accounts")
def account_create(
    request: Request,
    db: DB,
    _user: MasterUser,
    code: Annotated[str, Form(min_length=1, max_length=10)],
    name: Annotated[str, Form(min_length=1, max_length=50)],
    kind: Annotated[AccountKind, Form()],
):
    business_id = request.state.business.id
    code = code.strip()
    exists = select(Account.id).where(Account.business_id == business_id, Account.code == code)
    if db.scalar(exists):
        flash(request, f"계정 코드 {code}가 이미 있어요", "error")
        return _redirect("/master/accounts")
    db.add(Account(business_id=business_id, code=code, name=name.strip(), kind=kind))
    db.commit()
    flash(request, f"'{name}' 계정을 추가했어요")
    return _redirect("/master/accounts")


@router.post("/accounts/{account_id}")
def account_update(
    request: Request,
    db: DB,
    _user: MasterUser,
    account_id: int,
    code: Annotated[str, Form(min_length=1, max_length=10)],
    name: Annotated[str, Form(min_length=1, max_length=50)],
    is_active: Annotated[bool, Form()] = False,
):
    business_id = request.state.business.id
    acc = _owned(db, Account, account_id, business_id)
    if acc is None:
        return _redirect("/master/accounts")
    code = code.strip()
    dup = db.scalar(
        select(Account.id).where(
            Account.business_id == business_id, Account.code == code, Account.id != acc.id
        )
    )
    if dup:
        flash(request, f"계정 코드 {code}가 이미 있어요", "error")
        return _redirect("/master/accounts")
    if acc.system_key and not is_active:
        flash(request, f"'{acc.name}'은(는) 자동 분개에 쓰여서 끌 수 없어요", "error")
        return _redirect("/master/accounts")
    acc.code, acc.name, acc.is_active = code, name.strip(), is_active
    db.commit()
    flash(request, "저장했어요")
    return _redirect("/master/accounts")


# =====================================================================
# 엑셀 일괄 등록
# =====================================================================

IMPORTS = {
    "partners": ("거래처", PARTNER_COLUMNS, import_partners),
    "items": ("품목", ITEM_COLUMNS, import_items),
}


@router.get("/import")
def import_page(request: Request, _user: MasterUser):
    return render(request, "master/import.html", _ctx("import", imports=IMPORTS, result=None))


@router.get("/import/{kind}/template.xlsx")
def import_template(_user: MasterUser, kind: str):
    if kind not in IMPORTS:
        return _redirect("/master/import")
    label, columns, _ = IMPORTS[kind]
    example = {name: sample for name, _, sample in columns}
    cols = [(name, lambda r, n=name: r[n]) for name, _, _ in columns]
    return xlsx_response(f"{label}_일괄등록_양식", cols, [example])


@router.post("/import/{kind}")
def import_upload(
    request: Request,
    db: DB,
    _user: MasterUser,
    kind: str,
    file: UploadFile,
    tasks: BackgroundTasks,
):
    if kind not in IMPORTS:
        return _redirect("/master/import")
    label, _, importer = IMPORTS[kind]
    business = request.state.business
    result = importer(db, business, file.file)
    if result.ok and result.partner_ids:
        tasks.add_task(refresh_partner_statuses, business.id, result.partner_ids)
    ctx = _ctx(
        "import",
        imports=IMPORTS,
        result=result,
        result_kind=kind,
        result_label=label,
        filename=file.filename,
    )
    return render(request, "master/import.html", ctx, status_code=200 if result.ok else 400)
