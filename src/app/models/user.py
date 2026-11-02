from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class Role(StrEnum):
    OWNER = "owner"  # 대표(관리자)
    ACCOUNTANT = "accountant"  # 경리·회계
    SALES = "sales"  # 영업
    PURCHASING = "purchasing"  # 구매·자재
    TAX_AGENT = "tax_agent"  # 세무대리인(외부)


ROLE_LABELS = {
    Role.OWNER: "대표",
    Role.ACCOUNTANT: "경리·회계",
    Role.SALES: "영업",
    Role.PURCHASING: "구매·자재",
    Role.TAX_AGENT: "세무대리인",
}


class User(TenantMixin, TimestampMixin, Base):
    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 로그인 ID. 세무대리인의 다중 사업장 접근은 이후 별도 연결 테이블로 지원
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(50))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(String(20))
    # 2단계 인증: 간편 비밀번호(숫자 6자리) 해시. 대표는 필수, 승인할 때도 입력한다
    pin_hash: Mapped[str | None] = mapped_column(String(255))
    pin_failed: Mapped[int] = mapped_column(default=0)
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[Role(self.role)]
