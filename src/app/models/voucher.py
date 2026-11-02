"""거래: 전표(헤더) + 전표 품목(라인), 재고 이력, 월 마감(기획안 4·5장)"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin
from app.models.master import Item, Money, Partner, Qty, Unit, Warehouse


class VoucherKind(StrEnum):
    SALES = "sales"
    PURCHASE = "purchase"


VOUCHER_KIND_LABELS = {VoucherKind.SALES: "매출", VoucherKind.PURCHASE: "매입"}


class VoucherStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELED = "canceled"  # 마감 전 취소: 재고 이력도 함께 지운다
    REVERSED = "reversed"  # 마감 후 취소: 취소 전표(역분개)가 따로 있다


# 장부에 남는 전표: 확정 + 마감 후 취소된 원 전표(취소 전표의 음수와 상계)
EFFECTIVE_STATUSES = (VoucherStatus.CONFIRMED, VoucherStatus.REVERSED)

VOUCHER_STATUS_LABELS = {
    VoucherStatus.CONFIRMED: "확정",
    VoucherStatus.CANCELED: "취소",
    VoucherStatus.REVERSED: "취소 전표 발행",
}


class Voucher(TenantMixin, TimestampMixin, Base):
    """전표. 매출 전표 확정 = 출고(재고 차감), 매입 전표 확정 = 입고(재고 증가)."""

    __tablename__ = "voucher"
    __table_args__ = (
        UniqueConstraint("business_id", "number"),
        Index("ix_voucher_business_kind_date", "business_id", "kind", "voucher_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[VoucherKind] = mapped_column(String(10))
    number: Mapped[str] = mapped_column(String(20))  # S-20261102-0001
    voucher_date: Mapped[date] = mapped_column(Date)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partner.id"), index=True)
    status: Mapped[VoucherStatus] = mapped_column(String(10), default=VoucherStatus.CONFIRMED)
    supply_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=0)
    memo: Mapped[str | None] = mapped_column(Text)
    # 취소 전표가 가리키는 원 전표
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("voucher.id"))
    # 대사된 세금계산서(월합계 발행이면 여러 전표가 한 계산서를 가리킨다)
    tax_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("tax_invoice.id"), index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))

    partner: Mapped[Partner] = relationship()
    lines: Mapped[list["VoucherLine"]] = relationship(
        back_populates="voucher", cascade="all, delete-orphan", order_by="VoucherLine.line_no"
    )
    reversal_of: Mapped["Voucher | None"] = relationship(remote_side=[id])

    @property
    def kind_label(self) -> str:
        return VOUCHER_KIND_LABELS[VoucherKind(self.kind)]

    @property
    def status_label(self) -> str:
        if self.reversal_of_id:
            return "취소 전표"
        return VOUCHER_STATUS_LABELS[VoucherStatus(self.status)]

    @property
    def is_active(self) -> bool:
        return self.status == VoucherStatus.CONFIRMED


class VoucherLine(TenantMixin, Base):
    __tablename__ = "voucher_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("voucher.id"), index=True)
    line_no: Mapped[int]
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouse.id"))
    # 거래 단위와 수량·단가(예: 박스 10개 × 6,000원)
    unit_id: Mapped[int] = mapped_column(ForeignKey("unit.id"))
    qty: Mapped[Decimal] = mapped_column(Qty)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    # 등록 시점의 환산값(거래 단위 1 = 재고 단위 factor)과 재고 단위 수량
    factor: Mapped[Decimal] = mapped_column(Qty, default=1)
    stock_qty: Mapped[Decimal] = mapped_column(Qty)
    supply_amount: Mapped[Decimal] = mapped_column(Money)
    tax_exempt: Mapped[bool] = mapped_column(default=False)
    memo: Mapped[str | None] = mapped_column(String(200))

    voucher: Mapped[Voucher] = relationship(back_populates="lines")
    item: Mapped[Item] = relationship()
    unit: Mapped[Unit] = relationship()
    warehouse: Mapped[Warehouse] = relationship()


class StockMoveKind(StrEnum):
    PURCHASE_IN = "purchase_in"
    SALES_OUT = "sales_out"
    DISPOSAL = "disposal"  # 폐기
    ADJUST = "adjust"  # 실사 조정
    TRANSFER = "transfer"  # 창고 이동(보내는 창고 −, 받는 창고 +)


STOCK_MOVE_LABELS = {
    StockMoveKind.PURCHASE_IN: "매입 입고",
    StockMoveKind.SALES_OUT: "매출 출고",
    StockMoveKind.DISPOSAL: "폐기",
    StockMoveKind.ADJUST: "재고 조정",
    StockMoveKind.TRANSFER: "창고 이동",
}


class StockMove(TenantMixin, Base):
    """재고 이력. qty는 재고 단위 기준 부호 있는 수량(입고 +, 출고 −). 현재고 = 합계."""

    __tablename__ = "stock_move"
    __audit__ = False  # 전표·폐기·조정에서 파생 — 원본의 감사 로그로 추적
    __table_args__ = (Index("ix_stock_move_item_wh", "business_id", "item_id", "warehouse_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    move_date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[StockMoveKind] = mapped_column(String(20))
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouse.id"))
    qty: Mapped[Decimal] = mapped_column(Qty)
    # 재고 단위 하나의 원가(입고 시 매입단가 환산). 이동평균 단가 계산에 쓴다
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    voucher_id: Mapped[int | None] = mapped_column(ForeignKey("voucher.id"), index=True)
    voucher_line_id: Mapped[int | None] = mapped_column(ForeignKey("voucher_line.id"))
    disposal_id: Mapped[int | None] = mapped_column(ForeignKey("disposal.id"), index=True)
    adjustment_id: Mapped[int | None] = mapped_column(ForeignKey("stock_adjustment.id"), index=True)
    transfer_id: Mapped[int | None] = mapped_column(ForeignKey("stock_transfer.id"), index=True)

    item: Mapped[Item] = relationship()
    warehouse: Mapped[Warehouse] = relationship()
    voucher: Mapped[Voucher | None] = relationship()
    disposal: Mapped["Disposal | None"] = relationship()  # noqa: F821
    adjustment: Mapped["StockAdjustment | None"] = relationship()  # noqa: F821
    transfer: Mapped["StockTransfer | None"] = relationship()  # noqa: F821

    @property
    def kind_label(self) -> str:
        return STOCK_MOVE_LABELS[StockMoveKind(self.kind)]


class ClosingPeriod(TenantMixin, Base):
    """월 마감. 행이 있으면 그 달은 잠긴다."""

    __tablename__ = "closing_period"
    __table_args__ = (UniqueConstraint("business_id", "year_month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    year_month: Mapped[str] = mapped_column(String(7))  # 2026-11
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("user_account.id"))
