"""기준정보 규칙: 기본값 시드, 식별코드 정규화·채번, 품목그룹 트리, 단위·용량 표시,
거래 단위 일괄 관리."""

import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, Item, ItemGroup, ItemUnit, Unit, Warehouse
from app.models.master import UnitKind
from app.services.accounting import seed_accounts
from app.timeutil import today

UNCATEGORIZED_CODE = "UNCAT"

# (이름, 기호, 유형, 기준 단위 배수, 소수 자릿수)
DEFAULT_UNITS = [
    ("개", "ea", UnitKind.COUNT, 1, 0),
    ("병", "병", UnitKind.COUNT, 1, 0),
    ("박스", "box", UnitKind.COUNT, 1, 0),
    ("mL", "mL", UnitKind.VOLUME, 1, 0),
    ("L", "L", UnitKind.VOLUME, 1000, 3),
    ("g", "g", UnitKind.WEIGHT, 1, 0),
    ("kg", "kg", UnitKind.WEIGHT, 1000, 3),
    ("m", "m", UnitKind.LENGTH, 1, 2),
]


class MasterError(ValueError):
    """화면에 그대로 보여 줄 수 있는 업무 규칙 위반"""


def seed_business_defaults(db: Session, business: Business) -> None:
    """사업장 생성 시 기본 단위·미분류 그룹·기본 창고·계정과목을 만든다."""
    for order, (name, symbol, kind, ratio, decimals) in enumerate(DEFAULT_UNITS):
        db.add(
            Unit(
                business_id=business.id,
                name=name,
                symbol=symbol,
                kind=kind,
                base_ratio=Decimal(ratio),
                decimals=decimals,
                sort_order=order,
            )
        )
    db.add(
        ItemGroup(
            business_id=business.id,
            code=UNCATEGORIZED_CODE,
            code_key=UNCATEGORIZED_CODE,
            name="미분류",
            is_uncategorized=True,
            sort_order=9999,
        )
    )
    db.add(Warehouse(business_id=business.id, name="기본 창고", is_default=True))
    seed_accounts(db, business)


# ---------- 식별코드 ----------


def code_key(code: str) -> str:
    """대소문자·앞뒤 공백을 정리한 비교용 값(rm-2026-0001 == RM-2026-0001)."""
    return code.strip().upper()


_SEQ = re.compile(r"\{SEQ:(\d)\}")


def effective_code_format(business: Business, group: ItemGroup) -> str:
    node = group
    while node is not None:
        if node.code_format:
            return node.code_format
        node = node.parent
    return business.item_code_format


def next_item_code(db: Session, business: Business, group: ItemGroup) -> str:
    """형식 토큰: {GROUP} 그룹 식별코드, {YYYY}·{YY} 연도, {SEQ:n} n자리 일련번호."""
    fmt = effective_code_format(business, group)
    year = today().year
    rendered = (
        fmt.replace("{GROUP}", group.code)
        .replace("{YYYY}", str(year))
        .replace("{YY}", f"{year % 100:02d}")
    )
    m = _SEQ.search(rendered)
    if m is None:
        rendered += "-{SEQ:4}"
        m = _SEQ.search(rendered)
    width = int(m.group(1))
    prefix, suffix = rendered[: m.start()], rendered[m.end() :]
    pattern = re.compile(re.escape(code_key(prefix)) + r"(\d+)" + re.escape(code_key(suffix)) + "$")

    keys = db.scalars(
        select(Item.code_key).where(
            Item.business_id == business.id, Item.code_key.startswith(code_key(prefix))
        )
    )
    last = max((int(mm.group(1)) for k in keys if (mm := pattern.match(k))), default=0)
    return f"{prefix}{last + 1:0{width}d}{suffix}"


def ensure_unique_code(db: Session, model, business_id: int, code: str, exclude_id=None) -> None:
    stmt = select(model.id).where(
        model.business_id == business_id, model.code_key == code_key(code)
    )
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise MasterError(f"식별코드 '{code.strip()}'은(는) 이미 쓰고 있어요")


# ---------- 품목그룹 트리 ----------


def groups_of(db: Session, business_id: int) -> list[ItemGroup]:
    return list(
        db.scalars(
            select(ItemGroup)
            .where(ItemGroup.business_id == business_id)
            .order_by(ItemGroup.sort_order, ItemGroup.name)
        )
    )


def build_tree(groups: list[ItemGroup]) -> list[tuple[ItemGroup, int]]:
    """(그룹, 깊이) 목록을 트리 순서(부모 → 자식)로 펼친다."""
    children: dict[int | None, list[ItemGroup]] = {}
    for g in groups:
        children.setdefault(g.parent_id, []).append(g)
    out: list[tuple[ItemGroup, int]] = []

    def walk(parent_id, depth):
        for g in children.get(parent_id, []):
            out.append((g, depth))
            walk(g.id, depth + 1)

    walk(None, 0)
    return out


def group_options(db: Session, business_id: int, leaf_only: bool = False):
    """선택 상자용 (그룹, 깊이) 목록. leaf_only면 품목을 담을 수 있는 그룹만."""
    groups = groups_of(db, business_id)
    parents = {g.parent_id for g in groups}
    return [
        (g, depth)
        for g, depth in build_tree(groups)
        if not leaf_only or (g.id not in parents and g.is_active)
    ]


def path_label(group: ItemGroup) -> str:
    names = []
    node = group
    while node is not None:
        names.append(node.name)
        node = node.parent
    return " > ".join(reversed(names))


def descendant_ids(db: Session, group: ItemGroup) -> set[int]:
    """자신과 모든 하위 그룹 ID"""
    ids = {group.id}
    frontier = [group.id]
    while frontier:
        frontier = list(db.scalars(select(ItemGroup.id).where(ItemGroup.parent_id.in_(frontier))))
        ids.update(frontier)
    return ids


def _subtree_height(db: Session, group: ItemGroup) -> int:
    height, frontier = 0, [group.id]
    while True:
        frontier = list(db.scalars(select(ItemGroup.id).where(ItemGroup.parent_id.in_(frontier))))
        if not frontier:
            return height
        height += 1


def has_children(db: Session, group: ItemGroup) -> bool:
    return (
        db.scalar(select(ItemGroup.id).where(ItemGroup.parent_id == group.id).limit(1)) is not None
    )


def has_items(db: Session, group: ItemGroup) -> bool:
    return db.scalar(select(Item.id).where(Item.group_id == group.id).limit(1)) is not None


def validate_parent(
    db: Session, business: Business, group: ItemGroup | None, parent: ItemGroup | None
) -> int:
    """그룹을 parent 아래에 둘 수 있는지 확인하고 새 단계(level)를 돌려준다."""
    if parent is None:
        level = 1
    else:
        if parent.is_uncategorized:
            raise MasterError("미분류 그룹 아래에는 하위 그룹을 만들 수 없어요")
        if group is not None and parent.id in descendant_ids(db, group):
            raise MasterError("자기 자신이나 하위 그룹 아래로는 옮길 수 없어요")
        if has_items(db, parent):
            raise MasterError(f"'{parent.name}'에 품목이 있어 하위 그룹을 만들 수 없어요")
        level = parent.level + 1
    height = _subtree_height(db, group) if group is not None else 0
    if level + height > business.item_group_depth:
        raise MasterError(f"품목그룹은 {business.item_group_depth}단계까지 만들 수 있어요")
    return level


def relevel(db: Session, group: ItemGroup, level: int) -> None:
    group.level = level
    for child in db.scalars(select(ItemGroup).where(ItemGroup.parent_id == group.id)):
        relevel(db, child, level + 1)


def ensure_leaf(db: Session, group: ItemGroup) -> None:
    if has_children(db, group):
        raise MasterError(f"'{group.name}'에는 하위 그룹이 있어요. 가장 아래 그룹을 골라 주세요")


# ---------- 수량·용량 표시 ----------


def format_qty(value: Decimal | int | float | None, decimals: int = 0) -> str:
    if value is None:
        return ""
    d = Decimal(value).quantize(Decimal(1).scaleb(-decimals)) if decimals else Decimal(value)
    text = f"{d:,.{decimals}f}" if decimals else f"{int(d):,}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_content(total: Decimal, unit: Unit, units: list[Unit]) -> str:
    """총 용량을 같은 유형의 가장 큰 단위로 표시(60,000mL → 60L)."""
    base = Decimal(total) * unit.base_ratio
    same_kind = sorted(
        (u for u in units if u.kind == unit.kind and u.is_active),
        key=lambda u: u.base_ratio,
        reverse=True,
    )
    for u in same_kind:
        if base >= u.base_ratio:
            return f"{format_qty(base / u.base_ratio, 3)}{u.symbol}"
    return f"{format_qty(total, 3)}{unit.symbol}"


def stock_label(qty: Decimal, item: Item, units: list[Unit]) -> str:
    """재고 표시: '120병 · 60L'"""
    text = f"{format_qty(qty, item.stock_unit.decimals)}{item.stock_unit.symbol}"
    if qty and item.content_qty and item.content_unit:
        text += " · " + format_content(Decimal(qty) * item.content_qty, item.content_unit, units)
    return text


# ---------- 거래 단위 일괄 관리 ----------


def apply_trade_unit(
    business: Business,
    items: list[Item],
    unit: Unit,
    factor: Decimal,
    purchase_default: bool = False,
    sale_default: bool = False,
) -> tuple[int, int]:
    """여러 품목에 '1{unit} = factor 재고 단위'를 추가하거나 환산을 바꾼다.
    재고 단위가 같은 품목은 건너뛴다. 이미 등록된 전표는 등록 시점 환산값을 쓰므로 영향 없음.
    (적용, 건너뜀) 개수를 돌려준다."""
    if factor <= 0:
        raise MasterError("환산 수량은 0보다 커야 해요")
    applied = skipped = 0
    for item in items:
        if item.stock_unit_id == unit.id:
            skipped += 1
            continue
        existing = next((u for u in item.units if u.unit_id == unit.id), None)
        if existing is None:
            item.units.append(ItemUnit(business_id=business.id, unit_id=unit.id, factor=factor))
        else:
            existing.factor = factor
        if purchase_default:
            item.purchase_unit_id = unit.id
        if sale_default:
            item.sale_unit_id = unit.id
        applied += 1
    return applied, skipped


def remove_trade_unit(items: list[Item], unit: Unit) -> int:
    """거래 단위 환산을 지운다. 기본 거래 단위였으면 재고 단위로 돌린다. 지운 품목 수를 돌려준다."""
    removed = 0
    for item in items:
        existing = next((u for u in item.units if u.unit_id == unit.id), None)
        if existing is None:
            continue
        item.units.remove(existing)
        if item.purchase_unit_id == unit.id:
            item.purchase_unit_id = None
        if item.sale_unit_id == unit.id:
            item.sale_unit_id = None
        removed += 1
    return removed
