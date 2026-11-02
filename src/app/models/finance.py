"""자금: 수금·지급과 전표 배분, 세금계산서, 업로드 이력(기획안 5·6장)"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin
from app.models.master import Money, Partner
from app.models.user import User
from app.models.voucher import Voucher


class PaymentKind(StrEnum):
    RECEIPT = "receipt"  # 수금(매출 거래처)
    DISBURSEMENT = "disbursement"  # 지급(매입 거래처)


PAYMENT_KIND_LABELS = {PaymentKind.RECEIPT: "수금", PaymentKind.DISBURSEMENT: "지급"}


class PaymentMethod(StrEnum):
    TRANSFER = "transfer"
    CASH = "cash"
    CARD = "card"
    NOTE = "note"


PAYMENT_METHOD_LABELS = {
    PaymentMethod.TRANSFER: "계좌이체",
    PaymentMethod.CASH: "현금",
    PaymentMethod.CARD: "카드",
    PaymentMethod.NOTE: "어음",
}


class PaymentStatus(StrEnum):
    PENDING = "pending"  # 지급 승인 대기(기준금액 이상)
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANCELED = "canceled"
    REVERSED = "reversed"  # 마감 후 취소: 취소 건이 따로 있다


PAYMENT_STATUS_LABELS = {
    PaymentStatus.PENDING: "승인 대기",
    PaymentStatus.CONFIRMED: "확정",
    PaymentStatus.REJECTED: "반려",
    PaymentStatus.CANCELED: "취소",
    PaymentStatus.REVERSED: "취소 건 발행",
}

# 잔액에 반영되는 상태(마감 후 취소된 원 건은 음수 취소 건과 상계)
EFFECTIVE_PAYMENT_STATUSES = (PaymentStatus.CONFIRMED, PaymentStatus.REVERSED)


class Payment(TenantMixin, TimestampMixin, Base):
    """수금·지급. 전표와는 배분(PaymentAllocation)으로 N:M 연결한다."""

    __tablename__ = "payment"
    __table_args__ = (
        UniqueConstraint("business_id", "number"),
        Index("ix_payment_business_kind_date", "business_id", "kind", "payment_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[PaymentKind] = mapped_column(String(15))
    number: Mapped[str] = mapped_column(String(20))  # R-20261102-0001 / W-...
    payment_date: Mapped[date] = mapped_column(Date)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partner.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    method: Mapped[PaymentMethod] = mapped_column(String(10), default=PaymentMethod.TRANSFER)
    account_ref: Mapped[str | None] = mapped_column(String(100))  # 계좌·카드
    memo: Mapped[str | None] = mapped_column(Text)
    status: Mapped[PaymentStatus] = mapped_column(String(10), default=PaymentStatus.CONFIRMED)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("payment.id"))

    partner: Mapped[Partner] = relationship()
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    allocations: Mapped[list["PaymentAllocation"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", order_by="PaymentAllocation.id"
    )
    reversal_of: Mapped["Payment | None"] = relationship(remote_side=[id])

    @property
    def kind_label(self) -> str:
        return PAYMENT_KIND_LABELS[PaymentKind(self.kind)]

    @property
    def method_label(self) -> str:
        return PAYMENT_METHOD_LABELS[PaymentMethod(self.method)]

    @property
    def status_label(self) -> str:
        if self.reversal_of_id:
            return "취소 건"
        return PAYMENT_STATUS_LABELS[PaymentStatus(self.status)]

    @property
    def allocated(self) -> Decimal:
        return sum((a.amount for a in self.allocations), Decimal(0))


class PaymentAllocation(TenantMixin, Base):
    """수금·지급 금액을 어느 전표에 얼마씩 반영했는지(부분 수금 가능)"""

    __tablename__ = "payment_allocation"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payment.id"), index=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("voucher.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)

    payment: Mapped[Payment] = relationship(back_populates="allocations")
    voucher: Mapped[Voucher] = relationship()


class InvoiceDirection(StrEnum):
    SALES = "sales"  # 우리가 공급자
    PURCHASE = "purchase"  # 우리가 공급받는자


INVOICE_DIRECTION_LABELS = {InvoiceDirection.SALES: "매출", InvoiceDirection.PURCHASE: "매입"}


class TaxInvoice(TenantMixin, TimestampMixin, Base):
    """홈택스에서 내려받은 (전자)세금계산서. 전표와 대사한다."""

    __tablename__ = "tax_invoice"
    __table_args__ = (
        UniqueConstraint("business_id", "approval_no"),
        Index("ix_tax_invoice_business_date", "business_id", "issue_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[InvoiceDirection] = mapped_column(String(10))
    approval_no: Mapped[str] = mapped_column(String(40))
    issue_date: Mapped[date] = mapped_column(Date)  # 작성일자
    counterparty_biz_no: Mapped[str] = mapped_column(String(10))
    counterparty_name: Mapped[str | None] = mapped_column(String(100))
    supply_amount: Mapped[Decimal] = mapped_column(Money)
    vat_amount: Mapped[Decimal] = mapped_column(Money)
    total_amount: Mapped[Decimal] = mapped_column(Money)
    item_name: Mapped[str | None] = mapped_column(String(200))
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("upload_log.id"))
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    match_method: Mapped[str | None] = mapped_column(String(10))  # auto | manual

    vouchers: Mapped[list[Voucher]] = relationship(
        primaryjoin="TaxInvoice.id == foreign(Voucher.tax_invoice_id)",
        order_by="Voucher.voucher_date",
        viewonly=True,
    )

    @property
    def direction_label(self) -> str:
        return INVOICE_DIRECTION_LABELS[InvoiceDirection(self.direction)]

    @property
    def counterparty_biz_no_display(self) -> str:
        b = self.counterparty_biz_no
        return f"{b[:3]}-{b[3:5]}-{b[5:]}" if len(b) == 10 else b


class UploadLog(TenantMixin, TimestampMixin, Base):
    """업로드 이력: 파일명, 출처, 변환 건수, 실패 행"""

    __tablename__ = "upload_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))  # hometax
    filename: Mapped[str] = mapped_column(String(255))
    total_rows: Mapped[int] = mapped_column(default=0)
    created: Mapped[int] = mapped_column(default=0)
    duplicates: Mapped[int] = mapped_column(default=0)
    matched: Mapped[int] = mapped_column(default=0)
    # [[행 번호, 사유], ...]
    errors: Mapped[list | None] = mapped_column(JSON)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))

    created_by: Mapped[User | None] = relationship()
