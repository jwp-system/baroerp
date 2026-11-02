from enum import StrEnum

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TaxType(StrEnum):
    GENERAL = "general"  # 일반과세
    SIMPLIFIED = "simplified"  # 간이과세
    EXEMPT = "exempt"  # 면세


TAX_TYPE_LABELS = {
    TaxType.GENERAL: "일반과세",
    TaxType.SIMPLIFIED: "간이과세",
    TaxType.EXEMPT: "면세",
}


class Business(TimestampMixin, Base):
    """사업장"""

    __tablename__ = "business"

    id: Mapped[int] = mapped_column(primary_key=True)
    biz_no: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    # 거래명세서·세금계산서 공급자 정보
    ceo_name: Mapped[str | None] = mapped_column(String(50))
    biz_type: Mapped[str | None] = mapped_column(String(50))
    biz_item: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(30))
    tax_type: Mapped[TaxType] = mapped_column(String(20), default=TaxType.GENERAL)
    fiscal_end_month: Mapped[int] = mapped_column(default=12)
    payment_approval_threshold: Mapped[int] = mapped_column(default=1_000_000)
    # 역할별 메뉴 권한 조정값 {role: [menu, ...]}. 없으면 기본값(app.permissions)
    menu_permissions: Mapped[dict | None] = mapped_column(JSON)
    # 품목그룹 단계 수(1~3)와 품목 식별코드 기본 형식(app.services.codes)
    item_group_depth: Mapped[int] = mapped_column(default=3)
    item_code_format: Mapped[str] = mapped_column(String(50), default="{GROUP}-{SEQ:4}")
    # 재고보다 많이 출고(매출)하는 것을 허용할지
    allow_negative_stock: Mapped[bool] = mapped_column(default=False)
    # 폐기 손실을 매출원가에 포함할지(끄면 재고자산폐기손실 계정)
    disposal_to_cogs: Mapped[bool] = mapped_column(default=False)

    @property
    def biz_no_display(self) -> str:
        b = self.biz_no
        return f"{b[:3]}-{b[3:5]}-{b[5:]}"

    @property
    def charges_vat(self) -> bool:
        return self.tax_type != TaxType.EXEMPT
