"""전표 번호: {접두}-{YYYYMMDD}-{순번 4자리}. 사업장·날짜별로 1부터."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def next_number(db: Session, model, business_id: int, prefix: str, d: date) -> str:
    head = f"{prefix}-{d:%Y%m%d}-"
    last = db.scalar(
        select(func.max(model.number)).where(
            model.business_id == business_id, model.number.startswith(head)
        )
    )
    seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{head}{seq:04d}"
