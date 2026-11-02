from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin


class AuditLog(TenantMixin, Base):
    """감사 로그: 모든 등록·수정·삭제의 변경 전후 값"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
    action: Mapped[str] = mapped_column(String(10))  # create / update / delete
    target_table: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[int]
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
