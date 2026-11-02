"""기준정보 화면·엑셀 일괄 등록 테스트"""

import io
from html.parser import HTMLParser

from openpyxl import Workbook, load_workbook
from sqlalchemy import select, text

from app.models import Item, ItemGroup, Partner, Unit, Warehouse
from app.models.master import BizStatus
from app.services import nts
from app.services.excel_import import ITEM_COLUMNS, PARTNER_COLUMNS
from tests.conftest import csrf_of

VALID_BIZ_NO = "220-81-62007"


def _xlsx(columns, rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append([name for name, _, _ in columns])
    for row in rows:
        ws.append([row.get(name) for name, _, _ in columns])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client, kind, content: bytes):
    token = csrf_of(client, "/master/import")
    return client.post(
        f"/master/import/{kind}",
        data={"csrf_token": token},
        files={"file": ("upload.xlsx", content, "application/octet-stream")},
    )


# ---------- 거래처 ----------


def test_partner_create_checks_status_and_encrypts_contact(owner_client, db, monkeypatch):
    monkeypatch.setattr(nts, "fetch_statuses", lambda nos: {n: BizStatus.ACTIVE for n in nos})
    token = csrf_of(owner_client, "/master/partners/new")
    res = owner_client.post(
        "/master/partners",
        data={
            "name": "바로유통",
            "kind": "sales",
            "biz_no": VALID_BIZ_NO,
            "contact_phone": "010-1111-2222",
            "payment_terms_days": "45",
            "csrf_token": token,
        },
    )
    assert res.status_code == 303
    partner = db.scalar(select(Partner).where(Partner.name == "바로유통"))
    assert partner.biz_no == "2208162007"
    assert partner.contact_phone == "010-1111-2222"
    assert partner.biz_status == BizStatus.ACTIVE  # 등록 직후 백그라운드 조회
    raw = db.execute(text("select contact_phone from partner")).scalar()
    assert raw and "010" not in raw  # DB에는 암호문


def test_partner_rejects_invalid_and_duplicate_biz_no(owner_client, db):
    token = csrf_of(owner_client, "/master/partners/new")
    bad = owner_client.post(
        "/master/partners",
        data={"name": "A", "kind": "sales", "biz_no": "123-45-67890", "csrf_token": token},
    )
    assert bad.status_code == 400 and "없는 사업자번호예요" in bad.text
    short = owner_client.post(
        "/master/partners",
        data={"name": "A", "kind": "sales", "biz_no": "123-45-6789", "csrf_token": token},
    )
    assert short.status_code == 400 and "숫자 10자리" in short.text

    data = {"name": "A", "kind": "sales", "biz_no": VALID_BIZ_NO, "csrf_token": token}
    assert owner_client.post("/master/partners", data=data).status_code == 303
    dup = owner_client.post("/master/partners", data={**data, "name": "B"})
    assert dup.status_code == 400 and "이미 있어요" in dup.text


def test_partner_list_filter_shows_common_partners_in_both(owner_client, db, business):
    for name, kind in [("매출처", "sales"), ("매입처", "purchase"), ("공통처", "both")]:
        db.add(Partner(business_id=business.id, name=name, kind=kind))
    db.commit()
    html = owner_client.get("/master/partners?kind=purchase").text
    assert "매입처" in html and "공통처" in html and "매출처" not in html


# ---------- 품목 ----------


def test_item_create_with_auto_code_and_trade_unit(owner_client, db, business):
    group = ItemGroup(business_id=business.id, name="음료", code="BEV", code_key="BEV")
    db.add(group)
    db.commit()
    units = {u.name: u.id for u in db.scalars(select(Unit))}
    token = csrf_of(owner_client, "/master/items/new")
    res = owner_client.post(
        "/master/items",
        data={
            "name": "생수",
            "group_id": str(group.id),
            "stock_unit_id": str(units["병"]),
            "content_qty": "500",
            "content_unit_id": str(units["mL"]),
            "sale_price": "600",
            "trade_unit_id": [str(units["박스"])],
            "trade_factor": ["20"],
            "csrf_token": token,
        },
    )
    assert res.status_code == 303
    item = db.scalar(select(Item))
    assert item.code == "BEV-0001"
    assert [(u.unit.name, int(u.factor)) for u in item.units] == [("박스", 20)]

    html = owner_client.get("/master/items").text
    assert "BEV-0001" in html and "500mL" in html and "1박스=20병" in html


class _XData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        self.found += [v for k, v in attrs if k == "x-data"]


def test_item_form_alpine_data_survives_html_quoting(owner_client, db, setup):
    """x-data 안의 JSON(거래 단위·기본 단위)이 HTML 속성으로 온전히 전달돼야 한다.
    큰따옴표 속성에 tojson을 넣으면 속성이 중간에 끊겨 '거래 단위 추가'·기본 단위 선택이 멈춘다."""
    water = setup["water"]
    water.purchase_unit_id = water.units[0].unit_id
    db.commit()
    parser = _XData()
    parser.feed(owner_client.get(f"/master/items/{water.id}").text)
    data = parser.found[-1]
    assert "rows:" in data and "applyDefaults" in data and data.rstrip().endswith("}")
    assert f'purchaseUnit: "{water.units[0].unit_id}"' in data


def test_item_code_must_be_unique_case_insensitive(owner_client, db, business):
    uncat = db.scalar(select(ItemGroup).where(ItemGroup.is_uncategorized))
    unit = db.scalar(select(Unit).where(Unit.name == "개"))
    db.add(
        Item(
            business_id=business.id,
            code="RM-1",
            code_key="RM-1",
            name="A",
            group_id=uncat.id,
            stock_unit_id=unit.id,
        )
    )
    db.commit()
    token = csrf_of(owner_client, "/master/items/new")
    res = owner_client.post(
        "/master/items",
        data={
            "code": " rm-1 ",
            "name": "B",
            "group_id": str(uncat.id),
            "stock_unit_id": str(unit.id),
            "csrf_token": token,
        },
    )
    assert res.status_code == 400 and "이미 쓰고 있어요" in res.text


def test_group_with_items_is_deactivated_not_deleted(owner_client, db, business):
    group = ItemGroup(business_id=business.id, name="음료", code="BEV", code_key="BEV")
    db.add(group)
    db.flush()
    unit = db.scalar(select(Unit).where(Unit.name == "개"))
    db.add(
        Item(
            business_id=business.id,
            code="X",
            code_key="X",
            name="X",
            group_id=group.id,
            stock_unit_id=unit.id,
            sale_price=1000,
        )
    )
    db.commit()
    token = csrf_of(owner_client, f"/master/groups/{group.id}")

    owner_client.post(
        f"/master/groups/{group.id}/adjust-price",
        data={"target": "sale", "percent": "10", "csrf_token": token},
    )
    owner_client.post(f"/master/groups/{group.id}/delete", data={"csrf_token": token})
    db.expire_all()
    assert db.get(ItemGroup, group.id).is_active is False
    assert db.scalar(select(Item.sale_price)) == 1100


def test_only_one_default_warehouse(owner_client, db):
    token = csrf_of(owner_client, "/master/warehouses")
    owner_client.post("/master/warehouses", data={"name": "2창고", "csrf_token": token})
    second = db.scalar(select(Warehouse).where(Warehouse.name == "2창고"))
    owner_client.post(
        f"/master/warehouses/{second.id}",
        data={"name": "2창고", "is_active": "true", "is_default": "true", "csrf_token": token},
    )
    db.expire_all()
    defaults = db.scalars(select(Warehouse.name).where(Warehouse.is_default)).all()
    assert defaults == ["2창고"]


# ---------- 엑셀 일괄 등록 ----------


def test_import_partners_and_upsert_by_biz_no(owner_client, db, monkeypatch):
    monkeypatch.setattr(nts, "fetch_statuses", lambda nos: {})
    rows = [
        {"거래처명": "바로유통", "구분": "매출", "사업자번호": VALID_BIZ_NO, "결제조건(일)": 60},
        {"거래처명": "개인고객", "구분": "공통"},
    ]
    res = _upload(owner_client, "partners", _xlsx(PARTNER_COLUMNS, rows))
    assert res.status_code == 200 and "2건을 올렸어요" in res.text

    rows[0]["거래처명"] = "바로유통(주)"
    res = _upload(owner_client, "partners", _xlsx(PARTNER_COLUMNS, rows))
    assert "수정 2건" in res.text
    assert db.scalar(select(Partner.name).where(Partner.biz_no == "2208162007")) == "바로유통(주)"
    assert db.scalar(select(Partner.payment_terms_days).where(Partner.biz_no == "2208162007")) == 60


def test_import_rolls_back_everything_on_any_error(owner_client, db):
    rows = [
        {"거래처명": "정상", "구분": "매출"},
        {"거래처명": "구분 오류", "구분": "기타"},
        {"거래처명": "번호 오류", "구분": "매입", "사업자번호": "123-45-67890"},
    ]
    res = _upload(owner_client, "partners", _xlsx(PARTNER_COLUMNS, rows))
    assert res.status_code == 400
    assert "고칠 곳이 2군데" in res.text
    assert db.scalar(select(Partner)) is None


def test_import_items_creates_group_path_and_converts_units(owner_client, db):
    rows = [
        {
            "품목명": "생수 500mL",
            "품목그룹": "상품 > 음료 > 생수",
            "재고단위": "병",
            "단위용량": 500,
            "용량단위": "mL",
            "거래단위": "박스",
            "환산수량": 20,
            "판매단가": 600,
        },
        {"품목명": "탄산수", "품목그룹": "상품 > 음료 > 생수", "재고단위": "병"},
    ]
    res = _upload(owner_client, "items", _xlsx(ITEM_COLUMNS, rows))
    assert res.status_code == 200, res.text
    groups = {g.name: g for g in db.scalars(select(ItemGroup))}
    assert groups["생수"].level == 3 and groups["생수"].parent.name == "음료"
    items = db.scalars(select(Item).order_by(Item.id)).all()
    assert [i.code for i in items] == ["G0003-0001", "G0003-0002"]  # 같은 파일에서 순번 이어짐
    assert items[0].units[0].factor == 20


def test_import_items_missing_unit_and_header(owner_client, db):
    rows = [{"품목명": "A", "품목그룹": "미분류", "재고단위": "통"}]
    res = _upload(owner_client, "items", _xlsx(ITEM_COLUMNS, rows))
    assert "통&#39; 단위가 없어요" in res.text

    wb = Workbook()
    wb.active.append(["이름"])
    buf = io.BytesIO()
    wb.save(buf)
    res = _upload(owner_client, "items", buf.getvalue())
    assert "필수 열이 없어요" in res.text


def test_item_export_can_be_reimported(owner_client, db):
    rows = [{"품목명": "생수", "품목그룹": "음료", "재고단위": "병", "판매단가": 600}]
    _upload(owner_client, "items", _xlsx(ITEM_COLUMNS, rows))
    exported = owner_client.get("/master/items/export.xlsx").content
    ws = load_workbook(io.BytesIO(exported)).active
    header = [c.value for c in ws[1]]
    assert header[: len(ITEM_COLUMNS)] == [name for name, _, _ in ITEM_COLUMNS]

    res = _upload(owner_client, "items", exported)
    assert "수정 1건" in res.text
    assert db.scalar(select(Item).where(Item.name == "생수")) is not None


def test_import_template_download(owner_client):
    res = owner_client.get("/master/import/items/template.xlsx")
    ws = load_workbook(io.BytesIO(res.content)).active
    assert ws["B1"].value == "품목명" and ws["B2"].value == "생수 500mL"


def test_all_master_pages_render(owner_client, db, business):
    rows = [
        {
            "품목명": "생수",
            "품목그룹": "상품 > 음료",
            "재고단위": "병",
            "단위용량": 500,
            "용량단위": "mL",
            "거래단위": "박스",
            "환산수량": 20,
        }
    ]
    _upload(owner_client, "items", _xlsx(ITEM_COLUMNS, rows))
    db.add(Partner(business_id=business.id, name="P", kind="both", biz_no="2208162007"))
    db.commit()
    item = db.scalar(select(Item))
    partner = db.scalar(select(Partner))
    group = db.scalar(select(ItemGroup).where(ItemGroup.name == "상품"))
    for path in [
        "/master/partners",
        "/master/partners/new",
        f"/master/partners/{partner.id}",
        "/master/items",
        "/master/items/new",
        f"/master/items/{item.id}",
        f"/master/items/code-preview?group_id={item.group_id}",
        "/master/groups",
        f"/master/groups/{group.id}",
        "/master/units",
        "/master/warehouses",
        "/master/import",
        "/master/partners/export.xlsx",
        "/settings/audit?table=item",
    ]:
        res = owner_client.get(path)
        assert res.status_code == 200, path
    edit = owner_client.get(f"/master/items/{item.id}").text
    assert '"factor": "20"' in edit.replace("&#34;", '"')  # 거래 단위 행 복원


# ---------- 기본 거래 단위 ----------


def _item_post(owner_client, db, business, **extra):
    group = db.scalar(select(ItemGroup).where(ItemGroup.code_key == "BEV"))
    if group is None:
        group = ItemGroup(business_id=business.id, name="음료", code="BEV", code_key="BEV")
        db.add(group)
        db.commit()
    units = {u.name: u.id for u in db.scalars(select(Unit))}
    token = csrf_of(owner_client, "/master/items/new")
    data = {"name": "생수", "group_id": str(group.id), "stock_unit_id": str(units["병"]),
            "purchase_price": "300", "sale_price": "600",
            "trade_unit_id": [str(units["박스"])], "trade_factor": ["20"], "csrf_token": token,
            **{k: str(units[v]) if v in units else v for k, v in extra.items()}}  # fmt: skip
    return owner_client.post("/master/items", data=data), units


def test_item_default_trade_units_for_purchase_and_sales(owner_client, db, business):
    res, units = _item_post(owner_client, db, business, purchase_unit_id="박스", sale_unit_id="병")
    assert res.status_code == 303
    item = db.scalar(select(Item))
    # 매출 기본이 재고 단위(병)이면 비워 둔다
    assert (item.purchase_unit_id, item.sale_unit_id) == (units["박스"], None)
    assert "매입 박스 · 매출 병" in owner_client.get("/master/items").text

    buy = owner_client.get("/purchase/lookup/items?q=생수").json()[0]
    sell = owner_client.get("/sales/lookup/items?q=생수").json()[0]
    assert buy["default_unit_id"] == units["박스"] and buy["price"] == "300"
    assert sell["default_unit_id"] == units["병"]


def test_item_default_unit_must_be_stock_or_trade_unit(owner_client, db, business):
    res, _ = _item_post(owner_client, db, business, sale_unit_id="kg")
    assert res.status_code == 400 and "매출 기본 단위는" in res.text


def test_import_items_with_default_units(owner_client, db):
    rows = [{"품목명": "생수", "품목그룹": "음료", "재고단위": "병", "거래단위": "박스",
             "환산수량": 20, "매입기본단위": "박스", "매출기본단위": "병"}]  # fmt: skip
    res = _upload(owner_client, "items", _xlsx(ITEM_COLUMNS, rows))
    assert res.status_code == 200, res.text
    item = db.scalar(select(Item))
    box = db.scalar(select(Unit).where(Unit.name == "박스"))
    assert (item.purchase_unit_id, item.sale_unit_id) == (box.id, None)

    bad = [{"품목명": "물", "품목그룹": "음료", "재고단위": "병", "매입기본단위": "kg"}]
    res = _upload(owner_client, "items", _xlsx(ITEM_COLUMNS, bad))
    assert res.status_code == 400 and "매입기본단위는" in res.text


# ---------- 거래 단위 일괄 관리 ----------


def _items_for_trade(db, business):
    group = ItemGroup(business_id=business.id, name="음료", code="BEV", code_key="BEV")
    db.add(group)
    db.flush()
    units = {u.name: u for u in db.scalars(select(Unit))}
    items = []
    for code, name, stock in (("A1", "생수", "병"), ("A2", "주스", "병"), ("A3", "박스물", "박스")):
        item = Item(business_id=business.id, code=code, code_key=code, name=name,
                    group_id=group.id, stock_unit_id=units[stock].id)  # fmt: skip
        db.add(item)
        items.append(item)
    db.commit()
    return items, units


def _trade_post(owner_client, **data):
    token = csrf_of(owner_client, "/master/trade-units")
    return owner_client.post("/master/trade-units", data={"csrf_token": token, **data})


def test_trade_units_bulk_apply_update_and_remove(owner_client, db, business):
    (water, juice, boxed), units = _items_for_trade(db, business)
    box = units["박스"]
    page = owner_client.get("/master/trade-units")
    assert page.status_code == 200 and "생수" in page.text

    # 검색 결과 전체에 적용: 재고 단위가 박스인 품목은 건너뛴다
    res = _trade_post(owner_client, action="apply", scope="filtered", unit_id=str(box.id),
                      factor="20", purchase_default="true")  # fmt: skip
    assert res.status_code == 303
    db.expire_all()
    assert [(u.unit_id, u.factor) for u in water.units] == [(box.id, 20)]
    assert water.purchase_unit_id == box.id and water.sale_unit_id is None
    assert juice.units and not boxed.units
    assert "2개 품목에" in owner_client.get("/master/trade-units").text

    # 선택한 품목만 환산 수량 변경
    _trade_post(owner_client, action="apply", scope="selected", item_id=[str(juice.id)],
                unit_id=str(box.id), factor="12")  # fmt: skip
    db.expire_all()
    assert juice.units[0].factor == 12 and water.units[0].factor == 20

    listing = owner_client.get(f"/master/trade-units?unit={box.id}&has=with").text
    assert "1박스 = 12병" in listing and "박스물" not in listing

    # 지우면 기본 단위도 재고 단위로 돌아간다
    _trade_post(owner_client, action="remove", scope="selected", item_id=[str(water.id)],
                unit_id=str(box.id))  # fmt: skip
    db.expire_all()
    assert not water.units and water.purchase_unit_id is None and juice.units


def test_trade_units_validation(owner_client, db, business):
    (water, *_), units = _items_for_trade(db, business)
    box = str(units["박스"].id)
    _trade_post(owner_client, action="apply", scope="selected", unit_id=box, factor="20")
    assert "적용할 품목을 골라 주세요" in owner_client.get("/master/trade-units").text
    _trade_post(owner_client, action="apply", scope="selected", item_id=[str(water.id)],
                unit_id=box, factor="0")  # fmt: skip
    assert "0보다 커야" in owner_client.get("/master/trade-units").text
    db.expire_all()
    assert not water.units
