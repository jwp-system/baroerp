import hmac
import secrets

from fastapi import HTTPException, Request

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HEADER = "X-CSRF-Token"
FIELD = "csrf_token"


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    """폼(csrf_token 필드)·HTMX(X-CSRF-Token 헤더) 요청의 토큰을 세션 값과 비교한다.
    토큰 인증을 쓰는 /api·/tasks 경로는 제외."""
    if request.method in SAFE_METHODS or request.url.path.startswith(("/api/", "/tasks/")):
        return
    sent = request.headers.get(HEADER)
    if not sent:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            sent = (await request.form()).get(FIELD)
    expected = request.session.get("csrf")
    if not sent or not expected or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="CSRF 토큰이 올바르지 않아요")
