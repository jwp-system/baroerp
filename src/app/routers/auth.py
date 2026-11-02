from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth
from app.auth import PIN_SETUP_PATH, CurrentUser
from app.db import get_db
from app.models import Business, Role, User
from app.models.business import TAX_TYPE_LABELS, TaxType
from app.permissions import home_path
from app.security import hash_password, pin_format_error
from app.services.master import seed_business_defaults
from app.services.nts import biz_no_error
from app.templating import flash, render

router = APIRouter(include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]

MIN_PASSWORD = 8


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _has_business(db: Session) -> bool:
    return db.scalar(select(Business.id).limit(1)) is not None


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


# ---------- 초기 설정 ----------


@router.get("/setup")
def setup_form(request: Request, db: DB):
    if _has_business(db):
        return _redirect("/login")
    return render(request, "auth/setup.html", {"tax_types": TAX_TYPE_LABELS, "form": {}})


@router.post("/setup")
def setup(
    request: Request,
    db: DB,
    biz_name: Annotated[str, Form()],
    biz_no: Annotated[str, Form()],
    tax_type: Annotated[TaxType, Form()],
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    if _has_business(db):
        return _redirect("/login")

    form = {
        "biz_name": biz_name,
        "biz_no": biz_no,
        "tax_type": tax_type,
        "name": name,
        "email": email,
    }
    error = None
    if msg := biz_no_error(_digits(biz_no)):
        error = msg
    elif len(password) < MIN_PASSWORD:
        error = f"비밀번호는 {MIN_PASSWORD}자 이상이어야 해요"
    if error:
        ctx = {"tax_types": TAX_TYPE_LABELS, "form": form, "error": error}
        return render(request, "auth/setup.html", ctx, status_code=400)

    business = Business(biz_no=_digits(biz_no), name=biz_name.strip(), tax_type=tax_type)
    db.add(business)
    db.flush()
    owner = User(
        business_id=business.id,
        email=email.strip().lower(),
        name=name.strip(),
        password_hash=hash_password(password),
        role=Role.OWNER,
    )
    db.add(owner)
    seed_business_defaults(db, business)
    db.flush()
    auth.login(request, db, owner)
    # 대표는 2단계 인증(간편 비밀번호) 등록부터
    return _redirect(PIN_SETUP_PATH)


# ---------- 로그인 ----------


@router.get("/login")
def login_form(request: Request, db: DB):
    if not _has_business(db):
        return _redirect("/setup")
    return render(request, "auth/login.html", {"email": ""})


@router.post("/login")
def login(
    request: Request,
    db: DB,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    user = auth.authenticate(db, email, password)
    if user is None:
        ctx = {"email": email, "error": "이메일 또는 비밀번호가 맞지 않아요"}
        return render(request, "auth/login.html", ctx, status_code=400)

    if user.pin_hash:
        request.session.clear()
        request.session["pending_uid"] = user.id
        return _redirect("/login/pin")

    auth.login(request, db, user)
    return _redirect("/")


@router.get("/login/pin")
def pin_form(request: Request):
    if not request.session.get("pending_uid"):
        return _redirect("/login")
    return render(request, "auth/pin.html")


@router.post("/login/pin")
def pin(request: Request, db: DB, pin: Annotated[str, Form()]):
    uid = request.session.get("pending_uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.is_active or not user.pin_hash:
        return _redirect("/login")
    if error := auth.check_pin(db, user, pin):
        return render(request, "auth/pin.html", {"error": error}, status_code=400)
    auth.login(request, db, user)
    return _redirect("/")


@router.post("/logout")
def logout(request: Request):
    auth.logout(request)
    return _redirect("/login")


# ---------- 내 계정·2단계 인증 ----------


@router.get("/account")
def account(request: Request, user: CurrentUser):
    return render(request, "auth/account.html", {"active": "account"})


@router.post("/account/password")
def change_password(
    request: Request,
    db: DB,
    user: CurrentUser,
    current: Annotated[str, Form()],
    new: Annotated[str, Form()],
):
    if auth.authenticate(db, user.email, current) is None:
        flash(request, "현재 비밀번호가 맞지 않아요", "error")
    elif len(new) < MIN_PASSWORD:
        flash(request, f"새 비밀번호는 {MIN_PASSWORD}자 이상이어야 해요", "error")
    else:
        user.password_hash = hash_password(new)
        db.commit()
        flash(request, "비밀번호를 바꿨어요")
    return _redirect("/account")


def _pin_page(request: Request, user: User, error: str | None = None, status_code: int = 200):
    ctx = {
        "active": "account",
        "required": user.role == Role.OWNER and not user.pin_hash,
        "error": error,
    }
    return render(request, "auth/pin_setup.html", ctx, status_code=status_code)


@router.get(PIN_SETUP_PATH)
def pin_setup_form(request: Request, user: CurrentUser):
    return _pin_page(request, user)


@router.post(PIN_SETUP_PATH)
def pin_setup(
    request: Request,
    db: DB,
    user: CurrentUser,
    pin: Annotated[str, Form()],
    pin_confirm: Annotated[str, Form()],
    current: Annotated[str, Form()] = "",
):
    changing = bool(user.pin_hash)
    # 바꿀 때는 지금 간편 비밀번호부터 확인(로그인된 화면을 잠시 비운 사이 바꾸는 것 방지)
    error = auth.check_pin(db, user, current) if changing else None
    if error is None:
        error = pin_format_error(pin) or (
            "두 번 입력한 간편 비밀번호가 달라요" if pin != pin_confirm else None
        )
    if error:
        return _pin_page(request, user, error, status_code=400)
    user.pin_hash = hash_password(pin)
    user.pin_failed, user.pin_locked_until = 0, None
    db.commit()
    flash(request, "간편 비밀번호를 바꿨어요" if changing else "간편 비밀번호를 등록했어요")
    return _redirect(home_path(request.state.business, user.role))
