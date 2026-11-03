"""외부 예약 호출(Cloud Scheduler). Cloud Run은 요청이 없으면 꺼지므로 내장 스케줄러 대신 쓴다.
Authorization: Bearer <TASK_TOKEN> 으로 인증한다."""

import hmac

from fastapi import APIRouter, Header, HTTPException

from app.config import get_settings
from app.db import engine
from app.services import backup

router = APIRouter(prefix="/tasks", include_in_schema=False)


def _check(authorization: str | None) -> None:
    token = get_settings().task_token
    sent = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(sent, token):
        raise HTTPException(status_code=403)


@router.post("/backup")
def backup_task(authorization: str | None = Header(default=None)) -> dict:
    _check(authorization)
    return {"backup": backup.run_scheduled(engine)}


@router.post("/demo-reset")
def demo_reset_task(authorization: str | None = Header(default=None)) -> dict:
    """데모 사이트를 처음 상태로(매일 새벽 4시). 데모 모드가 아니면 없는 주소처럼 동작한다."""
    _check(authorization)
    if not get_settings().demo_mode:
        raise HTTPException(status_code=404)
    from app import demo

    return {"business": demo.reset().name}
