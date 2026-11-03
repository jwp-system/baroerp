from datetime import date, timedelta
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.auth import Forbidden, demo_locked, logout, require_menu
from app.config import get_settings
from app.db import engine, get_db
from app.export import xlsx_response
from app.models import AuditLog, Role, User
from app.models.business import TAX_TYPE_LABELS, TaxType
from app.models.user import ROLE_LABELS
from app.permissions import EDITABLE_ROLES, MENU_KEYS, MENUS, allowed_menus
from app.security import hash_password, verify_password
from app.services import backup, storage
from app.templating import flash, render
from app.timeutil import day_start_utc, to_local, today

router = APIRouter(prefix="/settings", include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]
Admin = Annotated[User, require_menu("settings")]

AUDIT_PAGE_SIZE = 50
TABLE_LABELS = {
    "business": "사업장",
    "user_account": "사용자",
    "partner": "거래처",
    "item": "품목",
    "item_group": "품목그룹",
    "item_unit": "거래 단위",
    "unit": "단위",
    "warehouse": "창고",
    "voucher": "전표",
    "voucher_line": "전표 품목",
    "closing_period": "월 마감",
    "account": "계정과목",
    "journal_entry": "분개",
    "journal_line": "분개 행",
    "disposal": "폐기",
    "stock_adjustment": "재고 조정",
    "stock_adjustment_line": "재고 조정 품목",
    "stock_transfer": "창고 이동",
    "stock_transfer_line": "창고 이동 품목",
    "attachment": "증빙",
    "payment": "수금·지급",
    "payment_allocation": "수금·지급 배분",
    "tax_invoice": "세금계산서",
    "upload_log": "업로드 이력",
}
ACTION_LABELS = {"create": "등록", "update": "수정", "delete": "삭제"}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- 사업장 ----------


@router.get("")
def business_form(request: Request, _user: Admin):
    ctx = {"active": "settings", "tab": "business", "tax_types": TAX_TYPE_LABELS}
    return render(request, "settings/business.html", ctx)


@router.post("")
def business_update(
    request: Request,
    db: DB,
    _user: Admin,
    name: Annotated[str, Form()],
    tax_type: Annotated[TaxType, Form()],
    fiscal_end_month: Annotated[int, Form(ge=1, le=12)],
    payment_approval_threshold: Annotated[int, Form(ge=0)],
    ceo_name: Annotated[str, Form()] = "",
    biz_type: Annotated[str, Form()] = "",
    biz_item: Annotated[str, Form()] = "",
    address: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    allow_negative_stock: Annotated[bool, Form()] = False,
    disposal_to_cogs: Annotated[bool, Form()] = False,
):
    business = request.state.business
    business.name = name.strip()
    business.ceo_name = ceo_name.strip() or None
    business.biz_type = biz_type.strip() or None
    business.biz_item = biz_item.strip() or None
    business.address = address.strip() or None
    business.phone = phone.strip() or None
    business.allow_negative_stock = allow_negative_stock
    business.disposal_to_cogs = disposal_to_cogs
    business.tax_type = tax_type
    business.fiscal_end_month = fiscal_end_month
    business.payment_approval_threshold = payment_approval_threshold
    db.commit()
    flash(request, "사업장 정보를 저장했어요")
    return _redirect("/settings")


# ---------- 사용자 ----------


@router.get("/users")
def users(request: Request, db: DB, _user: Admin):
    rows = db.scalars(
        select(User).where(User.business_id == request.state.business.id).order_by(User.id)
    ).all()
    ctx = {"active": "settings", "tab": "users", "users": rows, "roles": ROLE_LABELS}
    return render(request, "settings/users.html", ctx)


@router.post("/users")
def user_create(
    request: Request,
    db: DB,
    _user: Admin,
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    role: Annotated[Role, Form()],
    password: Annotated[str, Form(min_length=8)],
):
    email = email.strip().lower()
    if db.scalar(select(User.id).where(User.email == email)):
        flash(request, "이미 등록된 이메일이에요", "error")
        return _redirect("/settings/users")
    db.add(
        User(
            business_id=request.state.business.id,
            name=name.strip(),
            email=email,
            role=role,
            password_hash=hash_password(password),
        )
    )
    db.commit()
    flash(request, f"{name.strip()}님을 추가했어요")
    return _redirect("/settings/users")


def _own_user(db: Session, request: Request, user_id: int) -> User | None:
    target = db.get(User, user_id)
    if target is None or target.business_id != request.state.business.id:
        return None
    return target


@router.post("/users/{user_id}")
def user_update(
    request: Request,
    db: DB,
    me: Admin,
    user_id: int,
    role: Annotated[Role, Form()],
    is_active: Annotated[bool, Form()] = False,
    reset_2fa: Annotated[bool, Form()] = False,
):
    if demo_locked(request):
        return _redirect("/settings/users")
    target = _own_user(db, request, user_id)
    if target is None:
        flash(request, "사용자를 찾을 수 없어요", "error")
        return _redirect("/settings/users")
    if target.id == me.id and (role != Role.OWNER or not is_active):
        flash(request, "본인의 대표 권한이나 사용 상태는 바꿀 수 없어요", "error")
        return _redirect("/settings/users")

    target.role = role
    target.is_active = is_active
    if reset_2fa:
        target.pin_hash, target.pin_failed, target.pin_locked_until = None, 0, None
    db.commit()
    flash(request, f"{target.name}님 정보를 저장했어요")
    return _redirect("/settings/users")


# ---------- 메뉴 권한 ----------


@router.get("/permissions")
def permissions(request: Request, _user: Admin):
    business = request.state.business
    matrix = {role: allowed_menus(business, role) for role in EDITABLE_ROLES}
    ctx = {
        "active": "settings",
        "tab": "permissions",
        "roles": EDITABLE_ROLES,
        "role_labels": ROLE_LABELS,
        "menus": MENUS,
        "matrix": matrix,
    }
    return render(request, "settings/permissions.html", ctx)


@router.post("/permissions")
async def permissions_update(request: Request, db: DB, _user: Admin):
    if demo_locked(request):
        return _redirect("/settings/permissions")
    form = await request.form()
    overrides = {
        role.value: [m for m in MENU_KEYS if form.get(f"{role.value}:{m}")]
        for role in EDITABLE_ROLES
    }
    request.state.business.menu_permissions = overrides
    db.commit()
    flash(request, "메뉴 권한을 저장했어요")
    return _redirect("/settings/permissions")


@router.post("/permissions/reset")
def permissions_reset(request: Request, db: DB, _user: Admin):
    if demo_locked(request):
        return _redirect("/settings/permissions")
    request.state.business.menu_permissions = None
    db.commit()
    flash(request, "기본 권한으로 되돌렸어요")
    return _redirect("/settings/permissions")


# ---------- 감사 로그 ----------


def _audit_query(
    business_id: int, start: date, end: date, table: str, q: str
) -> Select[tuple[AuditLog, User | None]]:
    stmt = (
        select(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(
            AuditLog.business_id == business_id,
            AuditLog.created_at >= day_start_utc(start),
            AuditLog.created_at < day_start_utc(end + timedelta(days=1)),
        )
        .order_by(AuditLog.id.desc())
    )
    if table:
        stmt = stmt.where(AuditLog.target_table == table)
    if q:
        stmt = stmt.where(or_(User.name.contains(q), User.email.contains(q)))
    return stmt


class AuditFilter:
    def __init__(
        self,
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
        table: Annotated[str, Query()] = "",
        q: Annotated[str, Query()] = "",
        page: Annotated[int, Query(ge=1)] = 1,
    ):
        self.end = end or today()
        self.start = start or self.end - timedelta(days=30)
        self.table = table
        self.q = q.strip()
        self.page = page


Filter = Annotated[AuditFilter, Depends()]


@router.get("/audit")
def audit(request: Request, db: DB, _user: Admin, f: Filter):
    base = _audit_query(request.state.business.id, f.start, f.end, f.table, f.q)
    total = db.scalar(select(func.count()).select_from(base.order_by(None).subquery()))
    rows = db.execute(base.limit(AUDIT_PAGE_SIZE).offset((f.page - 1) * AUDIT_PAGE_SIZE)).all()
    ctx = {
        "active": "settings",
        "tab": "audit",
        "rows": rows,
        "f": f,
        "total": total,
        "pages": max(1, -(-total // AUDIT_PAGE_SIZE)),
        "table_labels": TABLE_LABELS,
        "action_labels": ACTION_LABELS,
    }
    # 필터 변경은 HTMX로 표 영역만 교체
    template = (
        "settings/_audit_table.html"
        if request.headers.get("HX-Request")
        else ("settings/audit.html")
    )
    return render(request, template, ctx)


@router.get("/audit/export.xlsx")
def audit_export(request: Request, db: DB, _user: Admin, f: Filter):
    rows = db.execute(_audit_query(request.state.business.id, f.start, f.end, f.table, f.q))
    columns = [
        ("일시", lambda r: to_local(r.AuditLog.created_at)),
        ("사용자", lambda r: r.User.name if r.User else "시스템"),
        ("작업", lambda r: ACTION_LABELS.get(r.AuditLog.action, r.AuditLog.action)),
        ("대상", lambda r: TABLE_LABELS.get(r.AuditLog.target_table, r.AuditLog.target_table)),
        ("대상 ID", lambda r: r.AuditLog.target_id),
        ("변경 전", lambda r: r.AuditLog.before),
        ("변경 후", lambda r: r.AuditLog.after),
    ]
    return xlsx_response("감사로그", columns, rows)


# ---------- 백업·복원(대표만) ----------

STORE_ERRORS = (storage.StorageError, httpx.HTTPError, OSError)


def _owner_only(user: User) -> None:
    if user.role != Role.OWNER or get_settings().is_saas:
        raise Forbidden()


@router.get("/backup")
def backup_page(request: Request, user: Admin):
    _owner_only(user)
    files, error = [], None
    try:
        files = backup.list_backups()
    except STORE_ERRORS:
        error = "백업 저장소를 읽지 못했어요. 저장소 설정을 확인해 주세요"
    ctx = {"active": "settings", "tab": "backup", "files": files, "error": error,
           "keep": get_settings().backup_keep}  # fmt: skip
    return render(request, "settings/backup.html", ctx)


@router.post("/backup")
def backup_create(request: Request, user: Admin):
    _owner_only(user)
    try:
        name = backup.run_scheduled(engine)
    except STORE_ERRORS:
        flash(request, "백업 파일을 저장하지 못했어요. 저장소 설정을 확인해 주세요", "error")
        return _redirect("/settings/backup")
    flash(request, f"백업했어요 · {name}")
    return _redirect("/settings/backup")


@router.get("/backup/files/{name}")
def backup_download(request: Request, user: Admin, name: str):
    _owner_only(user)
    data = backup.read_backup(name)
    if data is None:
        flash(request, "백업 파일을 찾을 수 없어요", "error")
        return _redirect("/settings/backup")
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})  # fmt: skip


@router.post("/backup/restore")
async def backup_restore(
    request: Request,
    db: DB,
    user: Admin,
    password: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
):
    _owner_only(user)
    if demo_locked(request):
        return _redirect("/settings/backup")
    if not verify_password(password, user.password_hash):
        flash(request, "비밀번호가 맞지 않아요", "error")
        return _redirect("/settings/backup")
    if file is not None and file.filename:
        data = await file.read()
    else:
        data = backup.read_backup(name) if name else None
    if not data:
        flash(request, "복원할 백업 파일을 골라 주세요", "error")
        return _redirect("/settings/backup")
    try:
        backup.check(engine, data)
        # 잘못 복원했을 때 되돌릴 수 있도록 지금 상태를 먼저 남긴다
        safety, current = backup.create(engine)
        backup.store_backup(safety.removesuffix(".zip") + "-before-restore.zip", current)
        db.close()
        manifest = backup.restore(engine, data)
        backup.prune()
    except backup.BackupError as exc:
        flash(request, str(exc), "error")
        return _redirect("/settings/backup")
    except STORE_ERRORS:
        flash(request, "백업 저장소에 쓰지 못해 복원하지 않았어요", "error")
        return _redirect("/settings/backup")
    logout(request)
    created = manifest["created_at"][:16].replace("T", " ")
    flash(request, f"{created} 백업으로 복원했어요. 다시 로그인해 주세요")
    return _redirect("/login")
