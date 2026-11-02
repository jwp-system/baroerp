from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Business, Role, User
from app.permissions import allowed_menus
from app.security import verify_password
from app.timeutil import to_local

PIN_SETUP_PATH = "/account/pin"
PIN_MAX_FAILS = 5
PIN_LOCK = timedelta(minutes=15)


class Redirect(Exception):
    """로그인·초기 설정·2단계 인증 등록이 필요할 때 해당 화면으로 보낸다."""

    def __init__(self, url: str):
        self.url = url


class Forbidden(Exception):
    pass


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return None
    return user


def check_pin(db: Session, user: User, pin: str) -> str | None:
    """간편 비밀번호 확인. 맞으면 None, 틀리면 안내 문구.
    연속으로 PIN_MAX_FAILS번 틀리면 PIN_LOCK 동안 입력을 막는다(6자리 무작위 대입 방지)."""
    now = datetime.now(UTC)
    if user.pin_locked_until and to_local(user.pin_locked_until) > now:
        until = to_local(user.pin_locked_until)
        return f"간편 비밀번호를 {PIN_MAX_FAILS}번 틀려서 {until:%H:%M}까지 입력할 수 없어요"
    if user.pin_hash and verify_password(pin.strip(), user.pin_hash):
        user.pin_failed, user.pin_locked_until = 0, None
        return None
    user.pin_failed = (user.pin_failed or 0) + 1
    if user.pin_failed >= PIN_MAX_FAILS:
        user.pin_failed, user.pin_locked_until = 0, now + PIN_LOCK
        db.commit()
        minutes = PIN_LOCK.seconds // 60
        return f"간편 비밀번호를 {PIN_MAX_FAILS}번 틀려서 {minutes}분 동안 입력할 수 없어요"
    db.commit()
    left = PIN_MAX_FAILS - user.pin_failed
    return f"간편 비밀번호가 맞지 않아요 ({left}번 더 틀리면 잠시 입력할 수 없어요)"


def check_approval_pin(db: Session, user: User, pin: str) -> str | None:
    """승인할 때 간편 비밀번호 확인. 등록한 사용자만 묻는다(대표는 등록이 필수)."""
    return check_pin(db, user, pin) if user.pin_hash else None


def login(request: Request, db: Session, user: User) -> None:
    # 세션 고정 공격 방지: 로그인 시 세션을 새로 시작
    request.session.clear()
    request.session["uid"] = user.id
    db.info["user_id"] = user.id
    user.last_login_at = datetime.now(UTC)
    db.commit()


def logout(request: Request) -> None:
    request.session.clear()


def get_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.is_active:
        if uid:  # 비활성·삭제된 사용자의 세션 정리(2단계 인증 대기 중인 세션은 유지)
            request.session.clear()
        if db.scalar(select(Business.id).limit(1)) is None:
            raise Redirect("/setup")
        raise Redirect("/login")

    # 대표(관리자)는 2단계 인증(간편 비밀번호) 필수
    if user.role == Role.OWNER and not user.pin_hash and request.url.path != PIN_SETUP_PATH:
        raise Redirect(PIN_SETUP_PATH)

    business = db.get(Business, user.business_id)
    db.info["user_id"] = user.id
    request.state.user = user
    request.state.business = business
    request.state.allowed = allowed_menus(business, user.role)
    return user


CurrentUser = Annotated[User, Depends(get_user)]


def require_menu(menu: str):
    def dependency(request: Request, user: CurrentUser) -> User:
        if menu not in request.state.allowed:
            raise Forbidden()
        return user

    return Depends(dependency)
