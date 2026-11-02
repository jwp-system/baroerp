from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.csrf import get_csrf_token
from app.permissions import MENUS
from app.timeutil import to_local

BASE_DIR = Path(__file__).parent

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["settings"] = get_settings()
templates.env.globals["menu"] = MENUS
templates.env.globals["csrf_token"] = get_csrf_token

# 모바일은 대시보드·영수증 등록·지급 승인만 제공(기획안 7장)
MOBILE_TABS = [
    ("dashboard", "홈", "/"),
    ("purchase", "영수증", "/purchase"),
    ("funds", "지급 승인", "/funds/approvals"),
    ("settings", "전체", "/settings"),
]
templates.env.globals["mobile_tabs"] = MOBILE_TABS


def won(value) -> str:
    return f"{int(value or 0):,}"


def datetime_ko(value) -> str:
    return to_local(value).strftime("%Y-%m-%d %H:%M") if value else "-"


templates.env.filters["won"] = won
templates.env.filters["dt"] = datetime_ko


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append({"message": message, "kind": kind})


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {
        "user": getattr(request.state, "user", None),
        "business": getattr(request.state, "business", None),
        "allowed": getattr(request.state, "allowed", set()),
        "flashes": request.session.pop("flash", []),
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
