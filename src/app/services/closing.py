"""월 마감 잠금. 마감된 달의 전표는 등록·수정·취소할 수 없고 취소 전표만 허용한다."""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ClosingPeriod
from app.timeutil import today


class ClosedPeriodError(ValueError):
    pass


def year_month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def closed_months(db: Session, business_id: int) -> set[str]:
    return set(
        db.scalars(select(ClosingPeriod.year_month).where(ClosingPeriod.business_id == business_id))
    )


def is_closed(db: Session, business_id: int, d: date) -> bool:
    return (
        db.scalar(
            select(ClosingPeriod.id).where(
                ClosingPeriod.business_id == business_id,
                ClosingPeriod.year_month == year_month(d),
            )
        )
        is not None
    )


def ensure_open(db: Session, business_id: int, d: date) -> None:
    if is_closed(db, business_id, d):
        raise ClosedPeriodError(f"{d.year}년 {d.month}월은 마감되어 바꿀 수 없어요")


def close_month(db: Session, business_id: int, ym: str, user_id: int | None) -> None:
    if ym > year_month(today()):
        raise ClosedPeriodError("아직 오지 않은 달은 마감할 수 없어요")
    if ym in closed_months(db, business_id):
        return
    db.add(
        ClosingPeriod(
            business_id=business_id,
            year_month=ym,
            closed_at=datetime.now(UTC),
            closed_by_id=user_id,
        )
    )


def reopen_month(db: Session, business_id: int, ym: str) -> None:
    row = db.scalar(
        select(ClosingPeriod).where(
            ClosingPeriod.business_id == business_id, ClosingPeriod.year_month == ym
        )
    )
    if row is not None:
        db.delete(row)
