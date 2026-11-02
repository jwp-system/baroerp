from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import app.audit  # noqa: F401  감사 로그 리스너 등록
from app.auth import Forbidden, Redirect
from app.config import get_settings
from app.csrf import csrf_protect
from app.routers import (
    auth,
    funds,
    health,
    inventory,
    ledger,
    master,
    pages,
    partners,
    reports,
    settings,
    tasks,
    tax_invoice,
    vouchers,
)
from app.scheduler import start_scheduler, stop_scheduler
from app.templating import BASE_DIR, render


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if get_settings().scheduler_enabled:
        start_scheduler()
    yield
    stop_scheduler()


def _redirect_handler(request: Request, exc: Redirect) -> Response:
    # HTMX 요청은 조각 대신 페이지 전체를 이동시킨다
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": exc.url})
    return RedirectResponse(exc.url, status_code=303)


def _forbidden_handler(request: Request, _exc: Forbidden) -> Response:
    return render(request, "forbidden.html", status_code=403)


def create_app() -> FastAPI:
    settings_ = get_settings()
    app = FastAPI(
        title=settings_.app_name,
        debug=settings_.debug,
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings_.secret_key,
        max_age=settings_.session_max_age,
        same_site="lax",
        https_only=not settings_.debug,
    )
    app.add_exception_handler(Redirect, _redirect_handler)
    app.add_exception_handler(Forbidden, _forbidden_handler)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(auth.router)
    app.include_router(settings.router)
    app.include_router(master.router)
    app.include_router(vouchers.sales_router)
    app.include_router(vouchers.purchase_router)
    app.include_router(inventory.router)
    app.include_router(funds.router)
    app.include_router(ledger.receivables_router)
    app.include_router(ledger.payables_router)
    app.include_router(partners.router)
    app.include_router(tax_invoice.router)
    app.include_router(reports.router)
    app.include_router(pages.router)
    return app


app = create_app()
