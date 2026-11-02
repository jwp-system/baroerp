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
