"""재고: 폐기 전표, 실사 재고 조정, 증빙 첨부(기획안 5장 폐기 처리)"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.accounting import JournalEntry
from app.models.base import Base, TenantMixin, TimestampMixin
from app.models.master import Item, Money, Qty, Warehouse
from app.models.user import User


class DisposalReason(StrEnum):
    EXPIRED = "expired"
    DAMAGED = "damaged"
    DEFECT = "defect"
    OTHER = "other"


DISPOSAL_REASON_LABELS = {
    DisposalReason.EXPIRED: "유통기한",
    DisposalReason.DAMAGED: "파손",
    DisposalReason.DEFECT: "품질 불량",
    DisposalReason.OTHER: "기타",
}


class DisposalStatus(StrEnum):
    PENDING = "pending"  # 폐기 대기: 격리 중, 재고에서는 아직 빠지지 않음
    APPROVED = "approved"  # 승인: 재고 차감 + 손실 분개
    CANCELED = "canceled"
    REVERSED = "reversed"  # 마감 후 취소: 취소 전표가 따로 있다


DISPOSAL_STATUS_LABELS = {
    DisposalStatus.PENDING: "승인 대기",
    DisposalStatus.APPROVED: "승인",
    DisposalStatus.CANCELED: "취소",
    DisposalStatus.REVERSED: "취소 전표 발행",
}


class Disposal(TenantMixin, TimestampMixin, Base):
    """폐기 전표. qty는 재고 단위, 취소 전표는 음수."""

    __tablename__ = "disposal"
    __table_args__ = (
        UniqueConstraint("business_id", "number"),
        Index("ix_disposal_business_date", "business_id", "disposal_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20))  # D-20261102-0001
    disposal_date: Mapped[date] = mapped_column(Date)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouse.id"))
    qty: Mapped[Decimal] = mapped_column(Qty)
    reason: Mapped[DisposalReason] = mapped_column(String(10))
    memo: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DisposalStatus] = mapped_column(String(10), default=DisposalStatus.PENDING)
    # 승인 시점의 이동평균 단가와 손실 금액
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    loss_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("disposal.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entry.id"))

    item: Mapped[Item] = relationship()
    warehouse: Mapped[Warehouse] = relationship()
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    reversal_of: Mapped["Disposal | None"] = relationship(remote_side=[id])
    journal_entry: Mapped[JournalEntry | None] = relationship()

    @property
    def reason_label(self) -> str:
        return DISPOSAL_REASON_LABELS[DisposalReason(self.reason)]

    @property
    def status_label(self) -> str:
        if self.reversal_of_id:
            return "취소 전표"
        return DISPOSAL_STATUS_LABELS[DisposalStatus(self.status)]


class AdjustmentStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELED = "canceled"
    REVERSED = "reversed"


ADJUSTMENT_STATUS_LABELS = {
    AdjustmentStatus.CONFIRMED: "확정",
    AdjustmentStatus.CANCELED: "취소",
    AdjustmentStatus.REVERSED: "취소 전표 발행",
}


class StockAdjustment(TenantMixin, TimestampMixin, Base):
    """실사 재고 조정. 실사 수량과 장부 수량의 차이만큼 재고를 맞추고 감모 손익을 분개한다."""

    __tablename__ = "stock_adjustment"
    __table_args__ = (UniqueConstraint("business_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20))  # A-20261130-0001
    count_date: Mapped[date] = mapped_column(Date, index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouse.id"))
    status: Mapped[AdjustmentStatus] = mapped_column(String(10), default=AdjustmentStatus.CONFIRMED)
    # 차이 금액 합계(감모는 음수)
    amount: Mapped[Decimal] = mapped_column(Money, default=0)
    memo: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("stock_adjustment.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entry.id"))

    warehouse: Mapped[Warehouse] = relationship()
    created_by: Mapped[User | None] = relationship()
    lines: Mapped[list["StockAdjustmentLine"]] = relationship(
        back_populates="adjustment",
        cascade="all, delete-orphan",
        order_by="StockAdjustmentLine.id",
    )
    reversal_of: Mapped["StockAdjustment | None"] = relationship(remote_side=[id])
    journal_entry: Mapped[JournalEntry | None] = relationship()

    @property
    def status_label(self) -> str:
        if self.reversal_of_id:
            return "취소 전표"
        return ADJUSTMENT_STATUS_LABELS[AdjustmentStatus(self.status)]


class StockAdjustmentLine(TenantMixin, Base):
    __tablename__ = "stock_adjustment_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    adjustment_id: Mapped[int] = mapped_column(ForeignKey("stock_adjustment.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"))
    book_qty: Mapped[Decimal] = mapped_column(Qty)
    counted_qty: Mapped[Decimal] = mapped_column(Qty)
    diff_qty: Mapped[Decimal] = mapped_column(Qty)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=0)
    amount: Mapped[Decimal] = mapped_column(Money, default=0)

    adjustment: Mapped[StockAdjustment] = relationship(back_populates="lines")
    item: Mapped[Item] = relationship()


class StockTransfer(TenantMixin, TimestampMixin, Base):
    """창고 이동. 원가·분개 없이 보내는 창고에서 빼고 받는 창고에 더한다."""

    __tablename__ = "stock_transfer"
    __table_args__ = (UniqueConstraint("business_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20))  # T-20261102-0001
    transfer_date: Mapped[date] = mapped_column(Date, index=True)
    from_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouse.id"))
    to_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouse.id"))
    # 확정·취소·취소 전표 발행(재고 조정과 같은 상태)
    status: Mapped[AdjustmentStatus] = mapped_column(String(10), default=AdjustmentStatus.CONFIRMED)
    memo: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transfer.id"))

    from_warehouse: Mapped[Warehouse] = relationship(foreign_keys=[from_warehouse_id])
    to_warehouse: Mapped[Warehouse] = relationship(foreign_keys=[to_warehouse_id])
    created_by: Mapped[User | None] = relationship()
    lines: Mapped[list["StockTransferLine"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", order_by="StockTransferLine.id"
    )
    reversal_of: Mapped["StockTransfer | None"] = relationship(remote_side=[id])

    @property
    def status_label(self) -> str:
        if self.reversal_of_id:
            return "취소 전표"
        return ADJUSTMENT_STATUS_LABELS[AdjustmentStatus(self.status)]


class StockTransferLine(TenantMixin, Base):
    __tablename__ = "stock_transfer_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("stock_transfer.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"))
    qty: Mapped[Decimal] = mapped_column(Qty)  # 재고 단위

    transfer: Mapped[StockTransfer] = relationship(back_populates="lines")
    item: Mapped[Item] = relationship()


class Attachment(TenantMixin, TimestampMixin, Base):
    """증빙 파일(사진·확인서·영수증). 파일은 저장소(app.services.storage)에 둔다."""

    __tablename__ = "attachment"
    __table_args__ = (Index("ix_attachment_owner", "business_id", "owner_type", "owner_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(20))  # disposal
    owner_id: Mapped[int]
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int]
    storage_key: Mapped[str] = mapped_column(String(255))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
