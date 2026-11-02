"""모든 등록·수정·삭제를 감사 로그로 남긴다(기획안 2장).

요청을 처리하는 사용자는 Session.info["user_id"]로 전달된다(app.auth에서 설정).
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import event, insert, inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import get_history

from app.models import AuditLog, Business
from app.models.base import TenantMixin

# 비밀값·암호화 저장하는 개인정보는 감사 로그에 원문을 남기지 않는다
MASKED_FIELDS = {
    "password_hash",
    "pin_hash",
    "contact_name",
    "contact_phone",
    "contact_email",
}
SKIPPED_FIELDS = {"created_at", "updated_at"}


def _jsonable(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _value(key, value):
    if key in MASKED_FIELDS:
        return "***" if value else None
    return _jsonable(value)


def _is_audited(obj) -> bool:
    # __audit__ = False: 다른 테이블에서 파생되는 행(재고 이력 등)은 기록하지 않는다
    return (
        isinstance(obj, TenantMixin | Business)
        and not isinstance(obj, AuditLog)
        and getattr(obj, "__audit__", True)
    )


def _snapshot(obj) -> dict:
    mapper = inspect(obj).mapper
    return {
        attr.key: _value(attr.key, getattr(obj, attr.key))
        for attr in mapper.column_attrs
        if attr.key not in SKIPPED_FIELDS
    }


def _changes(obj) -> tuple[dict, dict]:
    before, after = {}, {}
    for attr in inspect(obj).mapper.column_attrs:
        if attr.key in SKIPPED_FIELDS:
            continue
        hist = get_history(obj, attr.key)
        if not hist.has_changes():
            continue
        before[attr.key] = _value(attr.key, hist.deleted[0] if hist.deleted else None)
        after[attr.key] = _value(attr.key, hist.added[0] if hist.added else None)
    return before, after


def _business_id(obj) -> int:
    return obj.id if isinstance(obj, Business) else obj.business_id


@event.listens_for(Session, "after_flush")
def _write_audit(session: Session, _flush_context) -> None:
    rows = []
    for obj in session.new:
        if _is_audited(obj):
            rows.append(("create", obj, None, _snapshot(obj)))
    for obj in session.dirty:
        if _is_audited(obj) and session.is_modified(obj, include_collections=False):
            before, after = _changes(obj)
            if after:
                rows.append(("update", obj, before, after))
    for obj in session.deleted:
        if _is_audited(obj):
            rows.append(("delete", obj, _snapshot(obj), None))
    if not rows:
        return

    user_id = session.info.get("user_id")
    session.connection().execute(
        insert(AuditLog),
        [
            {
                "business_id": _business_id(obj),
                "user_id": user_id,
                "action": action,
                "target_table": obj.__tablename__,
                "target_id": obj.id,
                "before": before,
                "after": after,
            }
            for action, obj, before, after in rows
        ],
    )
