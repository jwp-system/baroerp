"""기준정보: 거래처, 품목그룹, 단위, 품목, 거래 단위 환산, 창고(기획안 3·5장)"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin
from app.models.types import EncryptedString

Qty = Numeric(18, 4)
Money = Numeric(15, 2)


# ---------- 거래처 ----------


class PartnerKind(StrEnum):
    SALES = "sales"
    PURCHASE = "purchase"
    BOTH = "both"


PARTNER_KIND_LABELS = {
    PartnerKind.SALES: "매출",
    PartnerKind.PURCHASE: "매입",
    PartnerKind.BOTH: "공통",
}


class BizStatus(StrEnum):
    UNKNOWN = "unknown"
    ACTIVE = "active"  # 계속사업자
    SUSPENDED = "suspended"  # 휴업
    CLOSED = "closed"  # 폐업
    UNREGISTERED = "unregistered"  # 국세청 미등록


BIZ_STATUS_LABELS = {
    BizStatus.UNKNOWN: "미확인",
    BizStatus.ACTIVE: "계속",
    BizStatus.SUSPENDED: "휴업",
    BizStatus.CLOSED: "폐업",
    BizStatus.UNREGISTERED: "미등록",
}


class Partner(TenantMixin, TimestampMixin, Base):
    """거래처"""

    __tablename__ = "partner"
    __table_args__ = (UniqueConstraint("business_id", "biz_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[PartnerKind] = mapped_column(String(10))
    # 개인(비사업자) 거래처는 비워 둔다
    biz_no: Mapped[str | None] = mapped_column(String(10))
    ceo_name: Mapped[str | None] = mapped_column(String(50))
    biz_type: Mapped[str | None] = mapped_column(String(50))  # 업태
    biz_item: Mapped[str | None] = mapped_column(String(50))  # 종목
    address: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(255))  # 세금계산서·거래명세서 수신
    contact_name: Mapped[str | None] = mapped_column(EncryptedString)
    contact_phone: Mapped[str | None] = mapped_column(EncryptedString)
    contact_email: Mapped[str | None] = mapped_column(EncryptedString)
    payment_terms_days: Mapped[int] = mapped_column(default=30)
    credit_limit: Mapped[Decimal | None] = mapped_column(Money)
    grade: Mapped[str | None] = mapped_column(String(1))  # 매입처 평가 등급 A~D(2차)
    biz_status: Mapped[BizStatus] = mapped_column(String(20), default=BizStatus.UNKNOWN)
    biz_status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)

    @property
    def kind_label(self) -> str:
        return PARTNER_KIND_LABELS[PartnerKind(self.kind)]

    @property
    def biz_status_label(self) -> str:
        return BIZ_STATUS_LABELS[BizStatus(self.biz_status)]

    @property
    def biz_no_display(self) -> str:
        b = self.biz_no
        return f"{b[:3]}-{b[3:5]}-{b[5:]}" if b else ""


# ---------- 단위 ----------


class UnitKind(StrEnum):
    COUNT = "count"
    VOLUME = "volume"
    WEIGHT = "weight"
    LENGTH = "length"


UNIT_KIND_LABELS = {
    UnitKind.COUNT: "개수",
    UnitKind.VOLUME: "부피",
    UnitKind.WEIGHT: "무게",
    UnitKind.LENGTH: "길이",
}


class Unit(TenantMixin, Base):
    """단위. 같은 유형끼리 base_ratio로 환산한다(1L = 1,000mL)."""

    __tablename__ = "unit"
    __table_args__ = (UniqueConstraint("business_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(10))
    kind: Mapped[UnitKind] = mapped_column(String(10))
    # 유형별 기준 단위(mL·g·m) 대비 배수. 개수 단위는 서로 환산하지 않으므로 1
    base_ratio: Mapped[Decimal] = mapped_column(Qty, default=1)
    decimals: Mapped[int] = mapped_column(default=0)
    sort_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)

    @property
    def kind_label(self) -> str:
        return UNIT_KIND_LABELS[UnitKind(self.kind)]


# ---------- 품목그룹·품목 ----------


class ItemGroup(TenantMixin, TimestampMixin, Base):
    """품목그룹(최대 3단계)"""

    __tablename__ = "item_group"
    __table_args__ = (UniqueConstraint("business_id", "code_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30))  # 식별코드(표시용)
    code_key: Mapped[str] = mapped_column(String(30))  # 대소문자·공백 정리한 비교용
    name: Mapped[str] = mapped_column(String(50))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("item_group.id"), index=True)
    level: Mapped[int] = mapped_column(default=1)
    sort_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    is_uncategorized: Mapped[bool] = mapped_column(default=False)
    # 품목 등록 시 기본값
    default_unit_id: Mapped[int | None] = mapped_column(ForeignKey("unit.id"))
    default_tax_exempt: Mapped[bool] = mapped_column(default=False)
    default_safety_stock: Mapped[Decimal | None] = mapped_column(Qty)
    code_format: Mapped[str | None] = mapped_column(String(50))

    parent: Mapped["ItemGroup | None"] = relationship(remote_side=[id])


class Item(TenantMixin, TimestampMixin, Base):
    """품목. id가 코드(시스템 식별), code가 사용자 지정 식별코드."""

    __tablename__ = "item"
    __table_args__ = (UniqueConstraint("business_id", "code_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30))
    code_key: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("item_group.id"), index=True)
    spec: Mapped[str | None] = mapped_column(String(100))  # 규격
    stock_unit_id: Mapped[int] = mapped_column(ForeignKey("unit.id"))
    # 재고 단위 하나의 용량(예: 1병 = 500mL)
    content_qty: Mapped[Decimal | None] = mapped_column(Qty)
    content_unit_id: Mapped[int | None] = mapped_column(ForeignKey("unit.id"))
    purchase_price: Mapped[Decimal] = mapped_column(Money, default=0)
    sale_price: Mapped[Decimal] = mapped_column(Money, default=0)
    tax_exempt: Mapped[bool] = mapped_column(default=False)
    safety_stock: Mapped[Decimal | None] = mapped_column(Qty)
    is_active: Mapped[bool] = mapped_column(default=True)
    # 전표 입력 시 기본 거래 단위(비우면 재고 단위). 재고 단위나 거래 단위 환산 중 하나
    purchase_unit_id: Mapped[int | None] = mapped_column(ForeignKey("unit.id"))
    sale_unit_id: Mapped[int | None] = mapped_column(ForeignKey("unit.id"))

    group: Mapped[ItemGroup] = relationship()
    stock_unit: Mapped[Unit] = relationship(foreign_keys=[stock_unit_id])
    content_unit: Mapped[Unit | None] = relationship(foreign_keys=[content_unit_id])
    units: Mapped[list["ItemUnit"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="ItemUnit.id"
    )


class ItemUnit(TenantMixin, Base):
    """거래 단위 환산(예: 1박스 = 20병). 전표 입력 시 재고 단위로 환산한다."""

    __tablename__ = "item_unit"
    __table_args__ = (UniqueConstraint("item_id", "unit_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"), index=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("unit.id"))
    factor: Mapped[Decimal] = mapped_column(Qty)  # 거래 단위 1 = 재고 단위 factor

    item: Mapped[Item] = relationship(back_populates="units")
    unit: Mapped[Unit] = relationship()


# ---------- 창고 ----------


class Warehouse(TenantMixin, TimestampMixin, Base):
    __tablename__ = "warehouse"
    __table_args__ = (UniqueConstraint("business_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
