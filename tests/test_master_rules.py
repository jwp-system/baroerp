"""기준정보 규칙(서비스 계층) 단위 테스트"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import Item, ItemGroup, Unit
from app.services import master as svc
from app.services.nts import biz_no_error


def _unit(db, business, name) -> Unit:
    return db.scalar(select(Unit).where(Unit.business_id == business.id, Unit.name == name))


def _group(db, business, name, code, parent=None) -> ItemGroup:
    level = svc.validate_parent(db, business, None, parent)
    g = ItemGroup(
        business_id=business.id,
        name=name,
        code=code,
        code_key=svc.code_key(code),
        parent_id=parent.id if parent else None,
        parent=parent,
        level=level,
    )
    db.add(g)
    db.flush()
    return g


def _item(db, business, group, code, unit_name="개") -> Item:
    item = Item(
        business_id=business.id,
        code=code,
        code_key=svc.code_key(code),
        name=code,
        group_id=group.id,
        stock_unit_id=_unit(db, business, unit_name).id,
    )
    db.add(item)
    db.flush()
    return item


def test_biz_no_checksum():
    assert biz_no_error("1234567815") is None
    assert biz_no_error("1234567890") == "없는 사업자번호예요. 번호를 다시 확인해 주세요"
    assert biz_no_error("123456781") == "사업자번호는 숫자 10자리로 입력해 주세요"


def test_biz_no_demo_mode_skips_checksum(monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_mode", True)
    assert biz_no_error("1234567890") is None
    assert biz_no_error("123456789") == "사업자번호는 숫자 10자리로 입력해 주세요"
    assert biz_no_error("12345678a0") == "사업자번호는 숫자 10자리로 입력해 주세요"


def test_code_key_ignores_case_and_spaces():
    assert svc.code_key("  rm-2026-0001 ") == svc.code_key("RM-2026-0001")


def test_next_item_code_uses_group_format_and_continues_sequence(db, business):
    rm = _group(db, business, "원자재", "RM")
    rm.code_format = "RM-{YYYY}-{SEQ:4}"
    year = svc.today().year
    assert svc.next_item_code(db, business, rm) == f"RM-{year}-0001"
    _item(db, business, rm, f"rm-{year}-0007")  # 대소문자 달라도 같은 순번 체계
    assert svc.next_item_code(db, business, rm) == f"RM-{year}-0008"


def test_child_group_inherits_parent_code_format(db, business):
    fg = _group(db, business, "제품", "FG")
    fg.code_format = "FG-{GROUP}-{SEQ:3}"
    a = _group(db, business, "A라인", "A", fg)
    assert svc.next_item_code(db, business, a) == "FG-A-001"


def test_default_format_uses_group_code(db, business):
    g = _group(db, business, "음료", "BEV")
    assert svc.next_item_code(db, business, g) == "BEV-0001"


def test_group_depth_limit(db, business):
    a = _group(db, business, "대", "L1")
    b = _group(db, business, "중", "L2", a)
    c = _group(db, business, "소", "L3", b)
    assert c.level == 3
    with pytest.raises(svc.MasterError, match="3단계"):
        _group(db, business, "너무 깊음", "L4", c)

    business.item_group_depth = 2
    with pytest.raises(svc.MasterError, match="2단계"):
        svc.validate_parent(db, business, None, b)


def test_items_only_in_leaf_and_no_children_under_group_with_items(db, business):
    parent = _group(db, business, "상품", "GD")
    child = _group(db, business, "음료", "DR", parent)
    with pytest.raises(svc.MasterError, match="하위 그룹"):
        svc.ensure_leaf(db, parent)
    svc.ensure_leaf(db, child)

    _item(db, business, child, "W-1")
    with pytest.raises(svc.MasterError, match="품목이 있어"):
        _group(db, business, "생수", "WT", child)


def test_cannot_move_group_under_its_descendant(db, business):
    a = _group(db, business, "대", "A1")
    b = _group(db, business, "중", "B1", a)
    with pytest.raises(svc.MasterError, match="하위 그룹 아래로"):
        svc.validate_parent(db, business, a, b)


def test_uncategorized_group_cannot_have_children(db, business):
    uncat = db.scalar(select(ItemGroup).where(ItemGroup.is_uncategorized))
    with pytest.raises(svc.MasterError, match="미분류"):
        svc.validate_parent(db, business, None, uncat)


def test_stock_label_shows_total_content_in_largest_unit(db, business):
    """데모 시나리오: 생수 500mL 10박스(1박스 = 20병) 입고 → '200병 · 100L'"""
    uncat = db.scalar(select(ItemGroup).where(ItemGroup.is_uncategorized))
    item = _item(db, business, uncat, "W-500", unit_name="병")
    item.content_qty = Decimal(500)
    item.content_unit = _unit(db, business, "mL")
    db.flush()
    db.refresh(item)
    units = list(db.scalars(select(Unit).where(Unit.business_id == business.id)))
    assert svc.stock_label(Decimal(200), item, units) == "200병 · 100L"
    assert svc.stock_label(Decimal(1), item, units) == "1병 · 500mL"
    assert svc.stock_label(Decimal(3), item, units) == "3병 · 1.5L"


def test_format_qty():
    assert svc.format_qty(Decimal("12.500"), 3) == "12.5"
    assert svc.format_qty(Decimal("1200"), 0) == "1,200"
