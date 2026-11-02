"""DB에는 UTC로 저장하고, 화면·필터는 한국 시간(KST)으로 다룬다."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def to_local(value: datetime) -> datetime:
    # SQLite는 시간대 없이 UTC 문자열로 돌려준다
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(KST)


def day_start_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=KST).astimezone(UTC)


def today() -> date:
    return datetime.now(KST).date()


def now_local() -> datetime:
    return datetime.now(KST)
