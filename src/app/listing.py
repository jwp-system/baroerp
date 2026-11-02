"""목록 화면 공통: 페이지 나누기, HTMX 부분 응답 선택."""

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

PAGE_SIZE = 50


@dataclass
class Page:
    rows: list
    total: int
    page: int
    pages: int


def paginate(db: Session, stmt: Select, page: int, size: int = PAGE_SIZE) -> Page:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    pages = max(1, -(-total // size))
    page = min(max(page, 1), pages)
    rows = list(db.execute(stmt.limit(size).offset((page - 1) * size)).all())
    return Page(rows=rows, total=total, page=page, pages=pages)


def pick_template(request: Request, full: str, partial: str) -> str:
    """필터 변경(HTMX)은 표 영역만 다시 그린다. 단, 다른 화면에서 HTMX로 이동해 온 경우는 전체."""
    if request.headers.get("HX-Request") and request.headers.get("HX-Target"):
        return partial
    return full
