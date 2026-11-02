"""전표·재고·마감 테스트. 기획안 데모 시나리오 3·4번(생수 입고 → 판매)을 따라간다."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Role, StockMove, Unit, Voucher
from app.models.business import TaxType
from app.models.voucher import VoucherKind, VoucherStatus
from app.services import closing
from app.services.master import stock_label
from app.services.voucher import LineInput, VoucherError, cancel_voucher, item_totals, save_voucher
from app.timeutil import today
from tests.conftest import csrf_of, login

S, P = VoucherKind.SALES, VoucherKind.PURCHASE


def _buy(db, business, s, boxes=10, d=None, price=6000):
    v = save_voucher(
        db, business, s["user"], P, None, d or today(), s["supplier"].id,
        [LineInput(s["water"].id, s["units"]["박스"].id, Decimal(boxes), Decimal(price))],
    )  # fmt: skip
    db.commit()
    return v


def _sell(db, business, s, bottles, d=None):
    v = save_voucher(
        db, business, s["user"], S, None, d or today(), s["customer"].id,
        [LineInput(s["water"].id, s["units"]["병"].id, Decimal(bottles), Decimal(600))],
    )  # fmt: skip
    db.commit()
    return v


def _stock(db, business, s) -> Decimal:
    return item_totals(db, business.id, [s["water"].id]).get(s["water"].id, Decimal(0))


# ---------- 저장 규칙 ----------


def test_purchase_in_boxes_increases_stock_in_bottles(db, business, setup):
    v = _buy(db, business, setup)
    assert v.number == f"P-{today():%Y%m%d}-0001"
    assert (v.supply_amount, v.vat_amount, v.total_amount) == (60000, 6000, 66000)
    line = v.lines[0]
    assert (line.factor, line.stock_qty) == (20, 200)
    units = list(db.scalars(select(Unit)))
    db.refresh(setup["water"])
    assert stock_label(_stock(db, business, setup), setup["water"], units) == "200병 · 100L"
    move = db.scalar(select(StockMove))
    assert move.unit_cost == 300  # 병당 원가 = 60,000 / 200


def test_sales_decreases_stock_and_blocks_shortage(db, business, setup):
    _buy(db, business, setup)
    v = _sell(db, business, setup, 50)
    assert v.number.startswith("S-")
    assert _stock(db, business, setup) == 150
    with pytest.raises(VoucherError, match="재고가 모자라요: 생수 500mL\\(기본 창고\\) 50병"):
        _sell(db, business, setup, 200)
    db.rollback()
    business.allow_negative_stock = True
    db.commit()
    _sell(db, business, setup, 200)
    assert _stock(db, business, setup) == -50


def test_vat_skips_exempt_items_and_exempt_business(db, business, setup):
    lines = [
        LineInput(setup["water"].id, setup["units"]["병"].id, Decimal(10), Decimal(600)),
        LineInput(setup["rice"].id, setup["units"]["kg"].id, Decimal("2.5"), Decimal(3000)),
    ]
    business.allow_negative_stock = True
    v = save_voucher(db, business, setup["user"], S, None, today(), setup["customer"].id, lines)
    assert (v.supply_amount, v.vat_amount) == (6000 + 7500, 600)
    assert v.lines[1].tax_exempt is True

    business.tax_type = TaxType.EXEMPT
    v2 = save_voucher(db, business, setup["user"], S, None, today(), setup["customer"].id, lines)
    assert v2.vat_amount == 0


def test_vat_is_truncated_to_won(db, business, setup):
    business.allow_negative_stock = True
    v = save_voucher(
        db, business, setup["user"], S, None, today(), setup["customer"].id,
        [LineInput(setup["water"].id, setup["units"]["병"].id, Decimal(1), Decimal(1999))],
    )  # fmt: skip
    assert v.vat_amount == 199


def test_fractional_qty_only_for_decimal_units(db, business, setup):
    with pytest.raises(VoucherError, match="소수 0자리"):
        save_voucher(
            db, business, setup["user"], P, None, today(), setup["supplier"].id,
            [LineInput(setup["water"].id, setup["units"]["병"].id, Decimal("1.5"), Decimal(1))],
        )  # fmt: skip


def test_partner_kind_must_match(db, business, setup):
    with pytest.raises(VoucherError, match="매출 거래처가 아니에요"):
        save_voucher(
            db, business, setup["user"], S, None, today(), setup["supplier"].id,
            [LineInput(setup["water"].id, setup["units"]["병"].id, Decimal(1), Decimal(1))],
        )  # fmt: skip
    db.rollback()
    business.allow_negative_stock = True
    # 공통 거래처는 매출·매입 모두 가능
    save_voucher(
        db, business, setup["user"], S, None, today(), setup["both"].id,
        [LineInput(setup["water"].id, setup["units"]["병"].id, Decimal(1), Decimal(1))],
    )  # fmt: skip


def test_unknown_trade_unit_rejected(db, business, setup):
    with pytest.raises(VoucherError, match="없는 거래 단위"):
        save_voucher(
            db, business, setup["user"], P, None, today(), setup["supplier"].id,
            [LineInput(setup["water"].id, setup["units"]["kg"].id, Decimal(1), Decimal(1))],
        )  # fmt: skip


def test_edit_replaces_stock_moves(db, business, setup):
    v = _buy(db, business, setup, boxes=10)
    save_voucher(
        db, business, setup["user"], P, v, today(), setup["supplier"].id,
        [LineInput(setup["water"].id, setup["units"]["박스"].id, Decimal(5), Decimal(6000))],
    )  # fmt: skip
    db.commit()
    assert _stock(db, business, setup) == 100
    assert len(db.scalars(select(StockMove)).all()) == 1


def test_edit_purchase_cannot_leave_negative_stock(db, business, setup):
    v = _buy(db, business, setup, boxes=10)
    _sell(db, business, setup, 150)
    with pytest.raises(VoucherError, match="재고가 모자라요"):
        save_voucher(
            db, business, setup["user"], P, v, today(), setup["supplier"].id,
            [LineInput(setup["water"].id, setup["units"]["박스"].id, Decimal(5), Decimal(6000))],
        )  # fmt: skip


# ---------- 취소·마감 ----------


def test_cancel_in_open_month_restores_stock(db, business, setup):
    _buy(db, business, setup)
    sale = _sell(db, business, setup, 30)
    assert cancel_voucher(db, business, setup["user"], sale) is None
    db.commit()
    assert sale.status == VoucherStatus.CANCELED
    assert _stock(db, business, setup) == 200
    with pytest.raises(VoucherError, match="이미 취소"):
        cancel_voucher(db, business, setup["user"], sale)


def test_closed_month_is_locked_and_cancel_creates_reversal(db, business, setup):
    last_month = today().replace(day=1) - timedelta(days=1)
    purchase = _buy(db, business, setup, d=last_month)
    closing.close_month(db, business.id, closing.year_month(last_month), None)
    db.commit()

    with pytest.raises(closing.ClosedPeriodError):
        _buy(db, business, setup, d=last_month)
    db.rollback()
    with pytest.raises(closing.ClosedPeriodError):
        save_voucher(
            db, business, setup["user"], P, purchase, today(), setup["supplier"].id,
            [LineInput(setup["water"].id, setup["units"]["박스"].id, Decimal(1), Decimal(1))],
        )  # fmt: skip
    db.rollback()

    reversal = cancel_voucher(db, business, setup["user"], purchase)
    db.commit()
    assert purchase.status == VoucherStatus.REVERSED
    assert reversal.voucher_date == today() and reversal.reversal_of_id == purchase.id
    assert (reversal.supply_amount, reversal.vat_amount) == (-60000, -6000)
    assert _stock(db, business, setup) == 0
    with pytest.raises(VoucherError, match="다시 취소"):
        cancel_voucher(db, business, setup["user"], reversal)


def test_cannot_close_future_month(db, business):
    next_month = (today().replace(day=28) + timedelta(days=5)).replace(day=1)
    with pytest.raises(closing.ClosedPeriodError):
        closing.close_month(db, business.id, closing.year_month(next_month), None)


# ---------- 화면 ----------


def _form(s, boxes="10", partner=None):
    return {
        "voucher_date": today().isoformat(),
        "partner_id": str((partner or s["supplier"]).id),
        "item_id": [str(s["water"].id), ""],  # 빈 줄은 무시
        "unit_id": [str(s["units"]["박스"].id), ""],
        "qty": [boxes, ""],
        "unit_price": ["6,000", ""],
        "warehouse_id": ["", ""],
        "line_memo": ["", ""],
    }


def test_purchase_flow_through_screens(owner_client, db, business, setup):
    token = csrf_of(owner_client, "/purchase/new")
    res = owner_client.post("/purchase", data={**_form(setup), "csrf_token": token})
    assert res.status_code == 303
    v = db.scalar(select(Voucher))
    assert res.headers["location"] == f"/purchase/{v.id}"
    assert v.total_amount == 66000

    edit = owner_client.get(f"/purchase/{v.id}")
    assert edit.status_code == 200 and "voucherForm" in edit.text

    lst = owner_client.get("/purchase").text
    assert v.number in lst and "66,000" in lst

    items = owner_client.get("/sales/lookup/items?q=생수").json()
    assert items[0]["stock"] == "200병 · 100L"
    assert items[0]["price"] == "600"
    assert [u["name"] for u in items[0]["units"]] == ["병", "박스"]

    partners = owner_client.get("/sales/lookup/partners").json()
    assert {p["name"] for p in partners} == {"편의점", "도매상"}

    assert owner_client.get("/purchase/export.xlsx").status_code == 200
    assert "60,000" in owner_client.get("/").text  # 대시보드 이번 달 매입


def test_sales_print_shows_supplier_and_lines(owner_client, db, business, setup):
    business.ceo_name = "박대표"
    business.allow_negative_stock = True
    db.commit()
    token = csrf_of(owner_client, "/sales/new")
    form = _form(setup, partner=setup["customer"])
    form["unit_id"][0] = str(setup["units"]["병"].id)
    owner_client.post("/sales", data={**form, "csrf_token": token})
    v = db.scalar(select(Voucher))
    html = owner_client.get(f"/sales/{v.id}/print").text
    assert "거래명세서" in html and "박대표" in html and "편의점" in html
    assert "생수 500mL" in html and "66,000" in html


def test_error_keeps_entered_lines(owner_client, setup):
    token = csrf_of(owner_client, "/sales/new")
    form = _form(setup, partner=setup["customer"])
    res = owner_client.post("/sales", data={**form, "csrf_token": token})
    assert res.status_code == 400
    assert "재고가 모자라요" in res.text
    assert "생수 500mL" in res.text  # 입력한 줄이 다시 채워진다


def test_cancel_and_view_through_screens(owner_client, db, setup):
    token = csrf_of(owner_client, "/purchase/new")
    owner_client.post("/purchase", data={**_form(setup), "csrf_token": token})
    v = db.scalar(select(Voucher))
    owner_client.post(f"/purchase/{v.id}/cancel", data={"csrf_token": token})
    page = owner_client.get(f"/purchase/{v.id}").text
    assert "취소된 전표예요" in page


def test_closing_permissions(client, make_user, business):
    ym = closing.year_month(today())
    login(client, make_user(Role.ACCOUNTANT))
    token = csrf_of(client, "/reports")
    client.post(f"/reports/closing/{ym}/close", data={"csrf_token": token})
    assert "마감 해제</button>" not in client.get("/reports").text
    client.post(f"/reports/closing/{ym}/reopen", data={"csrf_token": token})
    page = client.get("/reports").text
    assert "대표만" in page and 'badge-green">마감' in page


def test_tax_agent_cannot_close(client, make_user, db, business):
    login(client, make_user(Role.TAX_AGENT))
    token = csrf_of(client, "/reports")
    client.post(f"/reports/closing/{closing.year_month(today())}/close", data={"csrf_token": token})
    assert not closing.is_closed(db, business.id, today())
