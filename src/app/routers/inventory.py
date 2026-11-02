"""재고: 현황(가용·폐기 대기·총 용량), 이력, 폐기(등록·승인·손실 분개), 실사 조정"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import check_approval_pin, require_menu
from app.db import get_db
from app.export import xlsx_response
from app.listing import paginate, pick_template
from app.models import (
    Attachment,
    Disposal,
    Item,
    ItemGroup,
    Role,
    StockAdjustment,
    StockMove,
    StockTransfer,
    StockTransferLine,
    Unit,
    User,
    Warehouse,
)
from app.models.inventory import (
    ADJUSTMENT_STATUS_LABELS,
    DISPOSAL_REASON_LABELS,
    DISPOSAL_STATUS_LABELS,
    AdjustmentStatus,
    DisposalReason,
    DisposalStatus,
)
from app.models.voucher import STOCK_MOVE_LABELS, VoucherKind
from app.services import closing, storage
from app.services import inventory as svc
from app.services import master as master_svc
from app.services.accounting import AccountingError
from app.templating import flash, render
from app.timeutil import today

router = APIRouter(prefix="/inventory", include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]
InvUser = Annotated[User, require_menu("inventory")]

# 폐기 승인은 관리자(대표·경리)
CAN_APPROVE = {Role.OWNER, Role.ACCOUNTANT}
ERRORS = (svc.InventoryError, closing.ClosedPeriodError, AccountingError, storage.StorageError)

TABS = [
    ("stock", "재고 현황", "/inventory"),
    ("moves", "입·출고 이력", "/inventory/moves"),
    ("disposals", "폐기", "/inventory/disposals"),
    ("transfers", "창고 이동", "/inventory/transfers"),
    ("counts", "실사 조정", "/inventory/counts"),
]


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _ctx(tab: str, **kw) -> dict:
    return {"active": "inventory", "tabs": TABS, "tab": tab, "fmt_qty": master_svc.format_qty,
            **kw}  # fmt: skip


def _owned(db: Session, model, obj_id: int, business_id: int):
    obj = db.get(model, obj_id)
    return obj if obj is not None and obj.business_id == business_id else None


def _warehouses(db: Session, business_id: int, active_only: bool = True) -> list[Warehouse]:
    stmt = (
        select(Warehouse)
        .where(Warehouse.business_id == business_id)
        .order_by(Warehouse.is_default.desc(), Warehouse.name)
    )
    if active_only:
        stmt = stmt.where(Warehouse.is_active)
    return list(db.scalars(stmt))


def _units(db: Session, business_id: int) -> list[Unit]:
    return list(db.scalars(select(Unit).where(Unit.business_id == business_id)))


def _int(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _month_start() -> date:
    return today().replace(day=1)


# =====================================================================
# 재고 현황
# =====================================================================


class StockFilter:
    def __init__(
        self,
        q: Annotated[str, Query()] = "",
        group: Annotated[str, Query()] = "",
        warehouse: Annotated[str, Query()] = "",
        has_stock: Annotated[str, Query()] = "",
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        self.q, self.has_stock, self.page = q.strip(), has_stock, page
        self.group, self.warehouse = _int(group), _int(warehouse)
        self.start, self.end = start or _month_start(), end or today()

    def params(self) -> dict:
        return {"q": self.q, "group": self.group or "", "warehouse": self.warehouse or "",
                "has_stock": self.has_stock, "start": self.start, "end": self.end}  # fmt: skip


def _stock_query(db: Session, business_id: int, f: StockFilter):
    stmt = (
        select(Item)
        .where(Item.business_id == business_id)
        .options(
            selectinload(Item.group).selectinload(ItemGroup.parent),
            selectinload(Item.stock_unit),
            selectinload(Item.content_unit),
        )
        .order_by(Item.code_key)
    )
    if f.q:
        stmt = stmt.where(
            or_(Item.name.contains(f.q), Item.code_key.contains(master_svc.code_key(f.q)))
        )
    if f.group:
        group = _owned(db, ItemGroup, f.group, business_id)
        if group is not None:
            stmt = stmt.where(Item.group_id.in_(master_svc.descendant_ids(db, group)))
    if f.has_stock == "1":
        # 재고가 있거나 폐기 대기 중인 품목만
        moved = select(StockMove.item_id).where(StockMove.business_id == business_id)
        if f.warehouse:
            moved = moved.where(StockMove.warehouse_id == f.warehouse)
        moved = moved.group_by(StockMove.item_id).having(func.sum(StockMove.qty) != 0)
        pending = select(Disposal.item_id).where(
            Disposal.business_id == business_id, Disposal.status == DisposalStatus.PENDING
        )
        stmt = stmt.where(or_(Item.id.in_(moved), Item.id.in_(pending)))
    else:
        stmt = stmt.where(Item.is_active)
    return stmt


@router.get("")
def stock(request: Request, db: DB, _user: InvUser, f: Annotated[StockFilter, Depends()]):
    business_id = request.state.business.id
    page = paginate(db, _stock_query(db, business_id, f), f.page)
    rows = svc.stock_rows(db, business_id, [i for (i,) in page.rows], f.warehouse, f.start, f.end)
    ctx = _ctx(
        "stock",
        page=page,
        rows=rows,
        f=f,
        groups=master_svc.group_options(db, business_id),
        warehouses=_warehouses(db, business_id, active_only=False),
        units=_units(db, business_id),
        stock_label=master_svc.stock_label,
        path=master_svc.path_label,
    )
    return render(
        request, pick_template(request, "inventory/stock.html", "inventory/_stock_table.html"), ctx
    )


@router.get("/export.xlsx")
def stock_export(request: Request, db: DB, _user: InvUser, f: Annotated[StockFilter, Depends()]):
    business_id = request.state.business.id
    items = list(db.scalars(_stock_query(db, business_id, f)))
    rows = svc.stock_rows(db, business_id, items, f.warehouse, f.start, f.end)
    units = _units(db, business_id)

    def content(r):
        label = master_svc.stock_label(r.qty, r.item, units)
        return label.split(" · ", 1)[1] if " · " in label else ""

    columns = [
        ("식별코드", lambda r: r.item.code),
        ("품목명", lambda r: r.item.name),
        ("품목그룹", lambda r: master_svc.path_label(r.item.group)),
        ("재고단위", lambda r: r.item.stock_unit.name),
        ("현재고", lambda r: r.qty),
        ("폐기 대기", lambda r: r.pending),
        ("가용 재고", lambda r: r.available),
        ("총 용량", content),
        (f"기간 폐기({f.start}~{f.end})", lambda r: r.disposed),
    ]
    return xlsx_response("재고현황", columns, rows)


# =====================================================================
# 입·출고 이력
# =====================================================================


class MoveFilter:
    def __init__(
        self,
        q: Annotated[str, Query()] = "",
        warehouse: Annotated[str, Query()] = "",
        kind: Annotated[str, Query()] = "",
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        self.q, self.kind, self.page = q.strip(), kind, page
        self.warehouse = _int(warehouse)
        self.start, self.end = start or _month_start(), end or today()

    def params(self) -> dict:
        return {"q": self.q, "warehouse": self.warehouse or "", "kind": self.kind,
                "start": self.start, "end": self.end}  # fmt: skip


def _move_query(business_id: int, f: MoveFilter):
    stmt = (
        select(StockMove)
        .join(Item, Item.id == StockMove.item_id)
        .where(
            StockMove.business_id == business_id,
            StockMove.move_date >= f.start,
            StockMove.move_date <= f.end,
        )
        .options(
            selectinload(StockMove.item).selectinload(Item.stock_unit),
            selectinload(StockMove.warehouse),
            selectinload(StockMove.voucher),
            selectinload(StockMove.disposal),
            selectinload(StockMove.adjustment),
            selectinload(StockMove.transfer),
        )
        .order_by(StockMove.move_date.desc(), StockMove.id.desc())
    )
    if f.q:
        stmt = stmt.where(
            or_(Item.name.contains(f.q), Item.code_key.contains(master_svc.code_key(f.q)))
        )
    if f.warehouse:
        stmt = stmt.where(StockMove.warehouse_id == f.warehouse)
    if f.kind:
        stmt = stmt.where(StockMove.kind == f.kind)
    return stmt


def source_of(m: StockMove) -> tuple[str, str] | None:
    """(번호, 링크)"""
    if m.voucher is not None:
        prefix = "/sales" if m.voucher.kind == VoucherKind.SALES else "/purchase"
        return m.voucher.number, f"{prefix}/{m.voucher.id}"
    if m.disposal is not None:
        return m.disposal.number, f"/inventory/disposals/{m.disposal.id}"
    if m.adjustment is not None:
        return m.adjustment.number, f"/inventory/counts/{m.adjustment.id}"
    if m.transfer is not None:
        return m.transfer.number, f"/inventory/transfers/{m.transfer.id}"
    return None


@router.get("/moves")
def moves(request: Request, db: DB, _user: InvUser, f: Annotated[MoveFilter, Depends()]):
    business_id = request.state.business.id
    page = paginate(db, _move_query(business_id, f), f.page)
    ctx = _ctx(
        "moves",
        page=page,
        f=f,
        kinds=STOCK_MOVE_LABELS,
        warehouses=_warehouses(db, business_id, active_only=False),
        source_of=source_of,
    )
    return render(
        request, pick_template(request, "inventory/moves.html", "inventory/_move_table.html"), ctx
    )


@router.get("/moves/export.xlsx")
def moves_export(request: Request, db: DB, _user: InvUser, f: Annotated[MoveFilter, Depends()]):
    rows = db.scalars(_move_query(request.state.business.id, f))
    columns = [
        ("일자", lambda m: m.move_date),
        ("구분", lambda m: m.kind_label),
        ("식별코드", lambda m: m.item.code),
        ("품목명", lambda m: m.item.name),
        ("창고", lambda m: m.warehouse.name),
        ("수량", lambda m: m.qty),
        ("재고단위", lambda m: m.item.stock_unit.name),
        ("단가", lambda m: m.unit_cost),
        ("근거 전표", lambda m: (source_of(m) or ("",))[0]),
    ]
    return xlsx_response("재고이력", columns, rows)


# =====================================================================
# 폐기
# =====================================================================


class DisposalFilter:
    def __init__(
        self,
        q: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        reason: Annotated[str, Query()] = "",
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        self.q, self.status, self.reason, self.page = q.strip(), status, reason, page
        self.start, self.end = start or _month_start(), end or today()

    def params(self) -> dict:
        return {"q": self.q, "status": self.status, "reason": self.reason,
                "start": self.start, "end": self.end}  # fmt: skip


def _disposal_query(business_id: int, f: DisposalFilter):
    stmt = (
        select(Disposal)
        .join(Item, Item.id == Disposal.item_id)
        .where(
            Disposal.business_id == business_id,
            Disposal.disposal_date >= f.start,
            Disposal.disposal_date <= f.end,
        )
        .options(
            selectinload(Disposal.item).selectinload(Item.stock_unit),
            selectinload(Disposal.warehouse),
        )
        .order_by(Disposal.disposal_date.desc(), Disposal.number.desc())
    )
    if f.q:
        stmt = stmt.where(or_(Item.name.contains(f.q), Disposal.number.contains(f.q)))
    if f.status:
        stmt = stmt.where(Disposal.status == f.status)
    if f.reason:
        stmt = stmt.where(Disposal.reason == f.reason)
    return stmt


def _counts_as_loss(d: Disposal) -> bool:
    return d.status in (DisposalStatus.APPROVED, DisposalStatus.REVERSED)


@router.get("/disposals")
def disposals(request: Request, db: DB, _user: InvUser, f: Annotated[DisposalFilter, Depends()]):
    business_id = request.state.business.id
    page = paginate(db, _disposal_query(business_id, f), f.page)
    by_reason: dict[str, Decimal] = {}
    loss = Decimal(0)
    for d in db.scalars(_disposal_query(business_id, f)):
        if _counts_as_loss(d):
            loss += d.loss_amount
            by_reason[d.reason] = by_reason.get(d.reason, Decimal(0)) + d.loss_amount
    pending = db.scalar(
        select(func.count()).where(
            Disposal.business_id == business_id, Disposal.status == DisposalStatus.PENDING
        )
    )
    ctx = _ctx(
        "disposals",
        page=page,
        f=f,
        loss=loss,
        by_reason=by_reason,
        pending=pending,
        reasons=DISPOSAL_REASON_LABELS,
        statuses=DISPOSAL_STATUS_LABELS,
    )
    return render(
        request,
        pick_template(request, "inventory/disposals.html", "inventory/_disposal_table.html"),
        ctx,
    )


@router.get("/disposals/export.xlsx")
def disposals_export(
    request: Request, db: DB, _user: InvUser, f: Annotated[DisposalFilter, Depends()]
):
    rows = db.scalars(_disposal_query(request.state.business.id, f))
    columns = [
        ("일자", lambda d: d.disposal_date),
        ("폐기번호", lambda d: d.number),
        ("품목명", lambda d: d.item.name),
        ("창고", lambda d: d.warehouse.name),
        ("수량", lambda d: d.qty),
        ("재고단위", lambda d: d.item.stock_unit.name),
        ("사유", lambda d: d.reason_label),
        ("단가(이동평균)", lambda d: d.unit_cost),
        ("손실 금액", lambda d: d.loss_amount),
        ("상태", lambda d: d.status_label),
        ("메모", lambda d: d.memo),
    ]
    return xlsx_response("폐기내역", columns, rows)


def _active_items(db: Session, business_id: int) -> list[Item]:
    return list(
        db.scalars(
            select(Item)
            .where(Item.business_id == business_id, Item.is_active)
            .options(selectinload(Item.stock_unit))
            .order_by(Item.name)
        )
    )


def _disposal_form(request, db, form: dict | None = None, error=None, status=200):
    business_id = request.state.business.id
    ctx = _ctx(
        "disposals",
        items=_active_items(db, business_id),
        warehouses=_warehouses(db, business_id),
        reasons=DISPOSAL_REASON_LABELS,
        form=form or {"disposal_date": today().isoformat()},
        error=error,
    )
    return render(request, "inventory/disposal_form.html", ctx, status_code=status)


@router.get("/disposals/new")
def disposal_new(request: Request, db: DB, _user: InvUser, item: str = ""):
    return _disposal_form(request, db, {"disposal_date": today().isoformat(), "item_id": item})


class DisposalForm(BaseModel):
    disposal_date: date
    item_id: str = ""
    warehouse_id: str = ""
    qty: str = ""
    reason: DisposalReason = DisposalReason.DAMAGED
    memo: str = ""


def _real_files(files: list[UploadFile] | None) -> list[UploadFile]:
    return [f for f in files or [] if f.filename]


def _attach(db: Session, business_id: int, user: User, owner_id: int, files) -> int:
    count = 0
    for f in _real_files(files):
        data = f.file.read()
        key = storage.save(business_id, data, f.content_type or "")
        db.add(
            Attachment(
                business_id=business_id,
                owner_type="disposal",
                owner_id=owner_id,
                filename=(f.filename or "file")[:255],
                content_type=f.content_type or "",
                size=len(data),
                storage_key=key,
                uploaded_by_id=user.id,
            )
        )
        count += 1
    return count


def _decimal(value: str, label: str) -> Decimal:
    try:
        return Decimal((value or "").replace(",", "").strip())
    except InvalidOperation as exc:
        raise svc.InventoryError(f"{label}은(는) 숫자여야 해요") from exc


@router.post("/disposals")
def disposal_create(
    request: Request,
    db: DB,
    user: InvUser,
    form: Annotated[DisposalForm, Form()],
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    business = request.state.business
    try:
        d = svc.create_disposal(
            db, business, user, form.disposal_date, _int(form.item_id) or 0,
            _int(form.warehouse_id) or 0, _decimal(form.qty, "수량"), form.reason, form.memo,
        )  # fmt: skip
        _attach(db, business.id, user, d.id, files)
    except ERRORS as exc:
        db.rollback()
        return _disposal_form(request, db, form.model_dump(mode="json"), str(exc), 400)
    db.commit()
    flash(request, f"폐기를 등록했어요 · {d.number}. 승인하면 재고에서 빠져요")
    return _redirect(f"/inventory/disposals/{d.id}")


@router.get("/disposals/{disposal_id}")
def disposal_detail(request: Request, db: DB, user: InvUser, disposal_id: int):
    business_id = request.state.business.id
    d = _owned(db, Disposal, disposal_id, business_id)
    if d is None:
        return _redirect("/inventory/disposals")
    files = db.scalars(
        select(Attachment)
        .where(
            Attachment.business_id == business_id,
            Attachment.owner_type == "disposal",
            Attachment.owner_id == d.id,
        )
        .order_by(Attachment.id)
    ).all()
    ctx = _ctx(
        "disposals",
        d=d,
        files=files,
        can_approve=user.role in CAN_APPROVE,
        locked=closing.is_closed(db, business_id, d.disposal_date),
        reversal=db.scalar(select(Disposal).where(Disposal.reversal_of_id == d.id)),
        available=svc.stock_rows(db, business_id, [d.item], d.warehouse_id, today(), today())[0],
    )
    return render(request, "inventory/disposal_view.html", ctx)


def _disposal_action(request, db, user, disposal_id, action, done: str, approver_only=True):
    business = request.state.business
    d = _owned(db, Disposal, disposal_id, business.id)
    if d is None:
        return _redirect("/inventory/disposals")
    if approver_only and user.role not in CAN_APPROVE:
        flash(request, "폐기 승인·승인 취소는 대표·경리만 할 수 있어요", "error")
        return _redirect(f"/inventory/disposals/{d.id}")
    try:
        result = action(d)
    except ERRORS as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/inventory/disposals/{d.id}")
    db.commit()
    if isinstance(result, Disposal):
        flash(request, f"마감된 달이라 취소 전표 {result.number}를 만들었어요")
        return _redirect(f"/inventory/disposals/{result.id}")
    flash(request, done)
    return _redirect(f"/inventory/disposals/{d.id}")


@router.post("/disposals/{disposal_id}/approve")
def disposal_approve(
    request: Request, db: DB, user: InvUser, disposal_id: int, pin: Annotated[str, Form()] = ""
):
    if user.role in CAN_APPROVE and (error := check_approval_pin(db, user, pin)):
        flash(request, error, "error")
        return _redirect(f"/inventory/disposals/{disposal_id}")
    business = request.state.business
    return _disposal_action(
        request, db, user, disposal_id,
        lambda d: svc.approve_disposal(db, business, user, d),
        "승인했어요. 재고에서 빼고 손실을 분개했어요",
    )  # fmt: skip


@router.post("/disposals/{disposal_id}/unapprove")
def disposal_unapprove(request: Request, db: DB, user: InvUser, disposal_id: int):
    business = request.state.business
    return _disposal_action(
        request, db, user, disposal_id,
        lambda d: svc.unapprove_disposal(db, business, d),
        "승인을 취소했어요. 재고와 분개를 되돌렸어요",
    )  # fmt: skip


@router.post("/disposals/{disposal_id}/cancel")
def disposal_cancel(request: Request, db: DB, user: InvUser, disposal_id: int):
    business = request.state.business
    d = _owned(db, Disposal, disposal_id, business.id)
    # 승인 대기 건은 등록한 사람도 취소할 수 있다
    own_pending = (
        d is not None and d.status == DisposalStatus.PENDING and d.created_by_id == user.id
    )
    return _disposal_action(
        request, db, user, disposal_id,
        lambda d: svc.cancel_disposal(db, business, user, d),
        "폐기를 취소했어요",
        approver_only=not own_pending,
    )  # fmt: skip


@router.post("/disposals/{disposal_id}/attachments")
def disposal_attach(
    request: Request,
    db: DB,
    user: InvUser,
    disposal_id: int,
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    business_id = request.state.business.id
    d = _owned(db, Disposal, disposal_id, business_id)
    if d is None:
        return _redirect("/inventory/disposals")
    try:
        count = _attach(db, business_id, user, d.id, files)
    except storage.StorageError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/inventory/disposals/{d.id}")
    db.commit()
    flash(request, f"증빙 {count}개를 올렸어요" if count else "올릴 파일을 골라 주세요")
    return _redirect(f"/inventory/disposals/{d.id}")


@router.get("/attachments/{attachment_id}")
def attachment(request: Request, db: DB, _user: InvUser, attachment_id: int):
    a = _owned(db, Attachment, attachment_id, request.state.business.id)
    if a is None:
        return _redirect("/inventory/disposals")
    data = storage.read(a.storage_key)
    if data is None:
        flash(request, "파일을 찾을 수 없어요", "error")
        return _redirect(f"/inventory/disposals/{a.owner_id}")
    disposition = f"inline; filename*=UTF-8''{quote(a.filename)}"
    return Response(data, media_type=a.content_type, headers={"Content-Disposition": disposition})


# =====================================================================
# 실사 조정
# =====================================================================


@router.get("/counts")
def counts(request: Request, db: DB, _user: InvUser, page: Annotated[int, Query(ge=1)] = 1):
    business_id = request.state.business.id
    stmt = (
        select(StockAdjustment)
        .where(StockAdjustment.business_id == business_id)
        .options(selectinload(StockAdjustment.warehouse), selectinload(StockAdjustment.lines))
        .order_by(StockAdjustment.count_date.desc(), StockAdjustment.number.desc())
    )
    ctx = _ctx(
        "counts",
        page=paginate(db, stmt, page),
        warehouses=_warehouses(db, business_id),
        statuses=ADJUSTMENT_STATUS_LABELS,
        today=today(),
    )
    return render(request, "inventory/counts.html", ctx)


def _count_sheet(db: Session, business_id: int, warehouse: Warehouse, count_date: date):
    """실사표: 사용 중이거나 장부 수량이 있는 품목과 실사일 기준 장부 수량"""
    book = svc.stock_as_of(db, business_id, warehouse.id, count_date)
    items = list(
        db.scalars(
            select(Item)
            .where(Item.business_id == business_id, or_(Item.is_active, Item.id.in_(list(book))))
            .options(selectinload(Item.stock_unit), selectinload(Item.group))
            .order_by(Item.code_key)
        )
    )
    return [(item, book.get(item.id, Decimal(0))) for item in items]


@router.get("/counts/new")
def count_new(
    request: Request,
    db: DB,
    _user: InvUser,
    warehouse: str = "",
    count_date: date | None = None,
):
    business_id = request.state.business.id
    whs = _warehouses(db, business_id)
    wh = next((w for w in whs if w.id == _int(warehouse)), whs[0] if whs else None)
    count_date = count_date or today()
    ctx = _ctx(
        "counts",
        warehouses=whs,
        warehouse=wh,
        count_date=count_date,
        sheet=_count_sheet(db, business_id, wh, count_date) if wh else [],
        locked=closing.is_closed(db, business_id, count_date),
        form={},
        error=None,
    )
    return render(request, "inventory/count_form.html", ctx)


class CountForm(BaseModel):
    count_date: date
    warehouse_id: int
    memo: str = ""
    item_id: list[int] = []
    counted: list[str] = []


@router.post("/counts")
def count_create(request: Request, db: DB, user: InvUser, form: Annotated[CountForm, Form()]):
    business = request.state.business
    try:
        entered: dict[int, Decimal] = {}
        for item_id, value in zip(form.item_id, form.counted, strict=False):
            if value.strip():
                entered[item_id] = _decimal(value, "실사 수량")
        adj = svc.save_adjustment(
            db, business, user, form.count_date, form.warehouse_id, entered, form.memo
        )
    except ERRORS as exc:
        db.rollback()
        whs = _warehouses(db, business.id)
        wh = next((w for w in whs if w.id == form.warehouse_id), None)
        ctx = _ctx(
            "counts",
            warehouses=whs,
            warehouse=wh,
            count_date=form.count_date,
            sheet=_count_sheet(db, business.id, wh, form.count_date) if wh else [],
            locked=False,
            form=dict(zip(form.item_id, form.counted, strict=False)),
            memo=form.memo,
            error=str(exc),
        )
        return render(request, "inventory/count_form.html", ctx, status_code=400)
    db.commit()
    changed = sum(1 for ln in adj.lines if ln.diff_qty)
    flash(request, f"실사를 반영했어요 · {adj.number} · 차이 {changed}개 품목")
    return _redirect(f"/inventory/counts/{adj.id}")


@router.get("/counts/{adjustment_id}")
def count_detail(request: Request, db: DB, _user: InvUser, adjustment_id: int):
    business_id = request.state.business.id
    adj = _owned(db, StockAdjustment, adjustment_id, business_id)
    if adj is None:
        return _redirect("/inventory/counts")
    ctx = _ctx(
        "counts",
        adj=adj,
        locked=closing.is_closed(db, business_id, adj.count_date),
        reversal=db.scalar(select(StockAdjustment).where(StockAdjustment.reversal_of_id == adj.id)),
        can_cancel=adj.status == AdjustmentStatus.CONFIRMED and not adj.reversal_of_id,
    )
    return render(request, "inventory/count_view.html", ctx)


@router.post("/counts/{adjustment_id}/cancel")
def count_cancel(request: Request, db: DB, user: InvUser, adjustment_id: int):
    business = request.state.business
    adj = _owned(db, StockAdjustment, adjustment_id, business.id)
    if adj is None:
        return _redirect("/inventory/counts")
    if user.role not in CAN_APPROVE:
        flash(request, "재고 조정 취소는 대표·경리만 할 수 있어요", "error")
        return _redirect(f"/inventory/counts/{adj.id}")
    try:
        rev = svc.cancel_adjustment(db, business, user, adj)
    except ERRORS as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/inventory/counts/{adj.id}")
    db.commit()
    if rev is not None:
        flash(request, f"마감된 달이라 취소 전표 {rev.number}를 만들었어요")
        return _redirect(f"/inventory/counts/{rev.id}")
    flash(request, "재고 조정을 취소했어요. 재고와 분개를 되돌렸어요")
    return _redirect(f"/inventory/counts/{adj.id}")


# =====================================================================
# 창고 이동
# =====================================================================


@router.get("/transfers")
def transfers(request: Request, db: DB, _user: InvUser, page: Annotated[int, Query(ge=1)] = 1):
    business_id = request.state.business.id
    stmt = (
        select(StockTransfer)
        .where(StockTransfer.business_id == business_id)
        .options(
            selectinload(StockTransfer.from_warehouse),
            selectinload(StockTransfer.to_warehouse),
            selectinload(StockTransfer.lines).selectinload(StockTransferLine.item),
        )
        .order_by(StockTransfer.transfer_date.desc(), StockTransfer.number.desc())
    )
    ctx = _ctx(
        "transfers",
        page=paginate(db, stmt, page),
        warehouses=_warehouses(db, business_id),
        today=today(),
    )
    return render(request, "inventory/transfers.html", ctx)


def _transfer_sheet(db: Session, business_id: int, warehouse: Warehouse) -> list[svc.StockRow]:
    """보내는 창고에 재고가 있는 품목과 현재고·폐기 대기·가용"""
    held = select(StockMove.item_id).where(
        StockMove.business_id == business_id, StockMove.warehouse_id == warehouse.id
    )
    held = held.group_by(StockMove.item_id).having(func.sum(StockMove.qty) != 0)
    items = list(
        db.scalars(
            select(Item)
            .where(Item.business_id == business_id, Item.id.in_(held))
            .options(selectinload(Item.stock_unit))
            .order_by(Item.code_key)
        )
    )
    return svc.stock_rows(db, business_id, items, warehouse.id, today(), today())


def _transfer_form(request, db, src_id, dst_id, transfer_date, form=None, memo="", error=None):
    business_id = request.state.business.id
    whs = _warehouses(db, business_id)
    src = next((w for w in whs if w.id == src_id), whs[0] if whs else None)
    dst = next((w for w in whs if w.id == dst_id and w is not src), None) or next(
        (w for w in whs if w is not src), None
    )
    ctx = _ctx(
        "transfers",
        warehouses=whs,
        src=src,
        dst=dst,
        transfer_date=transfer_date,
        sheet=_transfer_sheet(db, business_id, src) if src else [],
        locked=closing.is_closed(db, business_id, transfer_date),
        form=form or {},
        memo=memo,
        error=error,
    )
    return render(request, "inventory/transfer_form.html", ctx, status_code=400 if error else 200)


@router.get("/transfers/new")
def transfer_new(
    request: Request,
    db: DB,
    _user: InvUser,
    src: str = "",
    dst: str = "",
    transfer_date: date | None = None,
):
    return _transfer_form(request, db, _int(src), _int(dst), transfer_date or today())


class TransferForm(BaseModel):
    transfer_date: date
    from_warehouse_id: int
    to_warehouse_id: int
    memo: str = ""
    item_id: list[int] = []
    qty: list[str] = []


@router.post("/transfers")
def transfer_create(request: Request, db: DB, user: InvUser, form: Annotated[TransferForm, Form()]):
    business = request.state.business
    entered = dict(zip(form.item_id, form.qty, strict=False))
    try:
        quantities = {i: _decimal(v, "이동 수량") for i, v in entered.items() if v.strip()}
        t = svc.save_transfer(
            db, business, user, form.transfer_date, form.from_warehouse_id,
            form.to_warehouse_id, quantities, form.memo,
        )  # fmt: skip
    except ERRORS as exc:
        db.rollback()
        return _transfer_form(
            request, db, form.from_warehouse_id, form.to_warehouse_id, form.transfer_date,
            entered, form.memo, str(exc),
        )  # fmt: skip
    db.commit()
    flash(request, f"창고 이동을 반영했어요 · {t.number} · {len(t.lines)}개 품목")
    return _redirect(f"/inventory/transfers/{t.id}")


@router.get("/transfers/{transfer_id}")
def transfer_detail(request: Request, db: DB, user: InvUser, transfer_id: int):
    business_id = request.state.business.id
    t = _owned(db, StockTransfer, transfer_id, business_id)
    if t is None:
        return _redirect("/inventory/transfers")
    ctx = _ctx(
        "transfers",
        t=t,
        locked=closing.is_closed(db, business_id, t.transfer_date),
        reversal=db.scalar(select(StockTransfer).where(StockTransfer.reversal_of_id == t.id)),
        can_cancel=user.role in CAN_APPROVE
        and t.status == AdjustmentStatus.CONFIRMED
        and not t.reversal_of_id,
    )
    return render(request, "inventory/transfer_view.html", ctx)


@router.post("/transfers/{transfer_id}/cancel")
def transfer_cancel(request: Request, db: DB, user: InvUser, transfer_id: int):
    business = request.state.business
    t = _owned(db, StockTransfer, transfer_id, business.id)
    if t is None:
        return _redirect("/inventory/transfers")
    if user.role not in CAN_APPROVE:
        flash(request, "창고 이동 취소는 대표·경리만 할 수 있어요", "error")
        return _redirect(f"/inventory/transfers/{t.id}")
    try:
        rev = svc.cancel_transfer(db, business, user, t)
    except ERRORS as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/inventory/transfers/{t.id}")
    db.commit()
    if rev is not None:
        flash(request, f"마감된 달이라 반대 방향 이동 {rev.number}를 만들었어요")
        return _redirect(f"/inventory/transfers/{rev.id}")
    flash(request, "창고 이동을 취소했어요. 재고를 원래 창고로 되돌렸어요")
    return _redirect(f"/inventory/transfers/{t.id}")
