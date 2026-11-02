"""서버 관리 명령. 화면으로 할 수 없는 일만 둔다.

uv run python -m app.manage reset-pin --email 이메일
    간편 비밀번호(2단계 인증) 초기화·잠금 해제. 대표는 다음 접속 때 새로 등록한다
"""

import argparse

from sqlalchemy import select

from app.models import Role, User


def reset_pin(db, email: str) -> User:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        raise SystemExit(f"{email} 사용자를 찾을 수 없어요")
    db.info["user_id"] = None  # 감사 로그에는 '시스템'으로 남는다
    user.pin_hash, user.pin_failed, user.pin_locked_until = None, 0, None
    db.commit()
    return user


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.manage", description="바로ERP 서버 관리")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-pin", help="간편 비밀번호(2단계 인증) 초기화·잠금 해제")
    r.add_argument("--email", required=True)
    args = parser.parse_args(argv)

    from app.db import SessionLocal

    with SessionLocal() as db:
        user = reset_pin(db, args.email)
        print(f"{user.name}({user.email})님의 간편 비밀번호를 초기화했어요")
        if user.role == Role.OWNER:
            print("다음에 로그인하면 간편 비밀번호를 새로 등록해요")


if __name__ == "__main__":
    main()
