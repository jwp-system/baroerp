"""역할별 메뉴 권한(기획안 2장). 사업장이 메뉴 단위로 조정할 수 있고, 대표는 항상 전체."""

from app.models import Business, Role

# (키, 이름, 경로) — 좌측 메뉴 순서
MENUS: list[tuple[str, str, str]] = [
    ("dashboard", "홈", "/"),
    ("sales", "매출", "/sales"),
    ("purchase", "매입", "/purchase"),
    ("inventory", "재고", "/inventory"),
    ("funds", "자금", "/funds"),
    ("receivables", "채권", "/receivables"),
    ("payables", "채무", "/payables"),
    ("partners", "거래처 상황판", "/partners"),
    ("tax_invoice", "세금계산서", "/tax-invoice"),
    ("reports", "보고서", "/reports"),
    ("master", "기준정보", "/master"),
    ("settings", "설정", "/settings"),
]
MENU_KEYS = [key for key, _, _ in MENUS]

DEFAULT_PERMISSIONS: dict[Role, set[str]] = {
    Role.OWNER: set(MENU_KEYS),
    Role.ACCOUNTANT: set(MENU_KEYS) - {"settings"},
    Role.SALES: {"partners", "sales", "receivables"},
    Role.PURCHASING: {"partners", "purchase", "inventory", "payables"},
    Role.TAX_AGENT: {"tax_invoice", "reports"},
}

# 대표 권한은 조정 불가(스스로 잠기는 것 방지)
EDITABLE_ROLES = [r for r in Role if r is not Role.OWNER]


def allowed_menus(business: Business, role: Role | str) -> set[str]:
    role = Role(role)
    if role is Role.OWNER:
        return set(MENU_KEYS)
    overrides = business.menu_permissions or {}
    if role.value in overrides:
        return set(overrides[role.value]) & set(MENU_KEYS)
    return set(DEFAULT_PERMISSIONS[role])


def home_path(business: Business, role: Role | str) -> str:
    allowed = allowed_menus(business, role)
    for key, _, path in MENUS:
        if key in allowed:
            return path
    return "/account"
