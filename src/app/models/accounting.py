"""계정과목과 분개. 폐기·재고 조정 손실을 자동 분개한다(기획안 5장 폐기 처리)."""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin
from app.models.master import Money


class AccountKind(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


ACCOUNT_KIND_LABELS = {
    AccountKind.ASSET: "자산",
    AccountKind.LIABILITY: "부채",
    AccountKind.EQUITY: "자본",
    AccountKind.REVENUE: "수익",
    AccountKind.EXPENSE: "비용",
}


class Account(TenantMixin, TimestampMixin, Base):
    """계정과목. system_key가 있는 계정은 자동 분개에 쓰이므로 끌 수 없다."""

    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("business_id", "code", name="uq_account_code"),
        UniqueConstraint("business_id", "system_key", name="uq_account_system_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(50))
    kind: Mapped[AccountKind] = mapped_column(String(10))
    system_key: Mapped[str | None] = mapped_column(String(30))
    is_active: Mapped[bool] = mapped_column(default=True)

    @property
    def kind_label(self) -> str:
        return ACCOUNT_KIND_LABELS[AccountKind(self.kind)]


class JournalSource(StrEnum):
    DISPOSAL = "disposal"
    ADJUSTMENT = "adjustment"


class JournalEntry(TenantMixin, TimestampMixin, Base):
    """분개 전표. 원천(폐기·재고 조정)에서 자동으로 만든다."""

    __tablename__ = "journal_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[JournalSource] = mapped_column(String(20))
    memo: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.line_no"
    )


class JournalLine(TenantMixin, Base):
    __tablename__ = "journal_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entry.id"), index=True)
    line_no: Mapped[int]
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    debit: Mapped[Decimal] = mapped_column(Money, default=0)
    credit: Mapped[Decimal] = mapped_column(Money, default=0)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()
