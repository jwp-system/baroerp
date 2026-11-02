"""거래처 상황판: 거래처별 이번 달 거래·잔액 목록과 거래처 하나의 홈 화면.
역할에 따라 매출 쪽·매입 쪽만 보인다."""

import re
from decimal import Decimal

from app.models import Role
from app.services.voucher import LineInput, save_voucher
from app.timeutil import today
from tests.conftest import login
from tests.test_vouchers import P, S, _buy, _sell


def _text(html: str) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"(\d) (원|건|병)", r"\1\2", text)


def test_board_for_owner_shows_both_sides(owner_client, db, business, setup):
    _buy(db, business, setup)  # 10박스 × 6,000 = 60,000(부가세 6,000)
    _sell(db, business, setup, 100)  # 100병 × 600 = 60,000
    bottle = setup["units"]["병"].id
    for kind, qty, price in ((S, 10, 600), (P, 5, 300)):
        line = LineInput(setup["water"].id, bottle, Decimal(qty), Decimal(price))
        save_voucher(db, business, setup["user"], kind, None, today(), setup["both"].id, [line])
    db.commit()

    page = _text(owner_client.get("/partners").text)
    assert "물공장" in page and "편의점" in page and "도매상" in page
    assert "이번 달 매출" in page and "미지급금" in page

    customer = _text(owner_client.get(f"/partners/{setup['customer'].id}").text)
    assert "이번 달 매출 60,000원" in customer
    assert "미수금 66,000원" in customer
    assert "생수 500mL 100병 60,000원" in customer  # 많이 매출한 품목
    assert "매출 전표" in customer  # 최근 거래
    assert "미지급금" not in customer  # 매출 거래처는 매입 쪽을 보여 주지 않는다

    both = _text(owner_client.get(f"/partners/{setup['both'].id}").text)
    assert "이번 달 매출 6,000원" in both and "이번 달 매입 1,500원" in both


def test_board_follows_role_sides(client, db, business, setup, make_user):
    _buy(db, business, setup)
    _sell(db, business, setup, 100)
    login(client, make_user(Role.SALES))

    page = _text(client.get("/partners").text)
    assert "편의점" in page and "물공장" not in page and "미지급금" not in page
    # 매입 전용 거래처는 볼 수 없다
    assert client.get(f"/partners/{setup['supplier'].id}").headers["location"] == "/partners"
    both = _text(client.get(f"/partners/{setup['both'].id}").text)
    assert "이번 달 매출" in both and "이번 달 매입" not in both
    # 권한 없는 메뉴(자금)로는 링크하지 않는다
    assert 'href="/funds' not in client.get(f"/partners/{setup['customer'].id}").text


def test_board_hidden_without_sales_or_purchase_menu(client, make_user, setup):
    login(client, make_user(Role.TAX_AGENT))
    assert client.get("/partners").status_code == 403
