"""S4 화면: 재고·폐기·실사, 자금·지급 승인, 채권·채무, 세금계산서, 계정과목, 대시보드"""

import re

import pytest
from sqlalchemy import select

from app.models import (
    Attachment,
    Disposal,
    Payment,
    Role,
    StockAdjustment,
    StockTransfer,
    TaxInvoice,
    Warehouse,
)
from app.models.finance import PaymentStatus
from app.models.inventory import DisposalStatus
from app.timeutil import today
from tests.conftest import PIN, csrf_of, login
from tests.test_tax_invoice import CUSTOMER, OUR, _row, _xlsx
from tests.test_vouchers import _buy, _sell

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def stocked(db, business, setup):
    setup["customer"].biz_no = CUSTOMER
    db.commit()
    _buy(db, business, setup)  # 생수 200병
    _sell(db, business, setup, 10)  # 6,600원 미수
    return setup


def _token(client, path):
    return csrf_of(client, path)


def test_inventory_pages(owner_client, db, stocked):
    for path in ["/inventory", "/inventory?has_stock=1", "/inventory/moves", "/inventory/disposals",
                 "/inventory/disposals/new", "/inventory/counts", "/inventory/counts/new",
                 "/inventory/export.xlsx", "/inventory/moves/export.xlsx",
                 "/inventory/disposals/export.xlsx"]:  # fmt: skip
        assert owner_client.get(path).status_code == 200, path
    html = owner_client.get("/inventory").text
    assert "190병 · 95L" in html  # 200 − 10병, 500mL
    assert "매출 출고" in owner_client.get("/inventory/moves").text


def test_disposal_flow_with_evidence(owner_client, db, stocked):
    wh = db.scalar(select(Warehouse).where(Warehouse.is_default))
    token = _token(owner_client, "/inventory/disposals/new")
    res = owner_client.post(
        "/inventory/disposals",
        data={"disposal_date": today().isoformat(), "item_id": stocked["water"].id,
              "warehouse_id": wh.id, "qty": "5", "reason": "damaged", "memo": "파손",
              "csrf_token": token},
        files=[("files", ("photo.png", PNG, "image/png"))],
    )  # fmt: skip
    assert res.status_code == 303
    d = db.scalar(select(Disposal))
    assert d.status == DisposalStatus.PENDING
    att = db.scalar(select(Attachment))
    assert att.owner_id == d.id and att.size == len(PNG)
    assert owner_client.get(f"/inventory/attachments/{att.id}").content == PNG

    page = owner_client.get(f"/inventory/disposals/{d.id}").text
    assert "승인하기" in page and "photo.png" in page
    token = _token(owner_client, f"/inventory/disposals/{d.id}")
    assert owner_client.post(f"/inventory/disposals/{d.id}/approve",
                             data={"pin": PIN, "csrf_token": token}).status_code == 303  # fmt: skip
    db.refresh(d)
    assert d.status == DisposalStatus.APPROVED and d.loss_amount == 1500
    page = owner_client.get(f"/inventory/disposals/{d.id}").text
    assert "재고자산폐기손실" in page and "승인 취소" in page
    assert "1,500" in owner_client.get("/").text  # 대시보드 폐기 손실


def test_disposal_rejects_bad_file_type(owner_client, db, stocked):
    wh = db.scalar(select(Warehouse).where(Warehouse.is_default))
    token = _token(owner_client, "/inventory/disposals/new")
    res = owner_client.post(
        "/inventory/disposals",
        data={"disposal_date": today().isoformat(), "item_id": stocked["water"].id,
              "warehouse_id": wh.id, "qty": "1", "reason": "other", "csrf_token": token},
        files=[("files", ("x.exe", b"MZ", "application/octet-stream"))],
    )  # fmt: skip
    assert res.status_code == 400 and "PDF만" in res.text
    assert db.scalar(select(Disposal)) is None


def test_purchasing_cannot_approve_disposal(client, db, business, stocked, make_user):
    wh = db.scalar(select(Warehouse).where(Warehouse.is_default))
    login(client, make_user(Role.PURCHASING))
    token = _token(client, "/inventory/disposals/new")
    client.post("/inventory/disposals", data={
        "disposal_date": today().isoformat(), "item_id": stocked["water"].id,
        "warehouse_id": wh.id, "qty": "1", "reason": "expired", "csrf_token": token})  # fmt: skip
    d = db.scalar(select(Disposal))
    assert "승인하기" not in client.get(f"/inventory/disposals/{d.id}").text
    client.post(f"/inventory/disposals/{d.id}/approve", data={"csrf_token": token})
    db.refresh(d)
    assert d.status == DisposalStatus.PENDING
    # 등록한 사람은 승인 대기 건을 취소할 수 있다
    client.post(f"/inventory/disposals/{d.id}/cancel", data={"csrf_token": token})
    db.refresh(d)
    assert d.status == DisposalStatus.CANCELED


def test_stock_count_form(owner_client, db, stocked):
    wh = db.scalar(select(Warehouse).where(Warehouse.is_default))
    page = owner_client.get(f"/inventory/counts/new?warehouse={wh.id}").text
    assert "190병" in page
    token = _token(owner_client, "/inventory/counts")
    res = owner_client.post("/inventory/counts", data={
        "count_date": today().isoformat(), "warehouse_id": wh.id, "memo": "월말",
        "item_id": [stocked["water"].id, stocked["rice"].id], "counted": ["185", ""],
        "csrf_token": token})  # fmt: skip
    assert res.status_code == 303
    adj = db.scalar(select(StockAdjustment))
    assert len(adj.lines) == 1 and adj.lines[0].diff_qty == -5
    assert "재고자산감모손실" in owner_client.get(f"/inventory/counts/{adj.id}").text


def test_receipt_flow_and_ledgers(owner_client, db, stocked):
    partner = stocked["customer"]
    page = owner_client.get(f"/funds/open-vouchers?kind=receipt&partner_id={partner.id}").text
    assert "6,600" in page
    token = _token(owner_client, "/funds/new")
    res = owner_client.post("/funds", data={
        "kind": "receipt", "payment_date": today().isoformat(), "partner_id": partner.id,
        "amount": "5,000", "method": "transfer", "csrf_token": token})  # fmt: skip
    assert res.status_code == 303
    p = db.scalar(select(Payment))
    assert p.allocated == 5000
    assert "선수금" not in owner_client.get(f"/funds/{p.id}").text
    for path in ["/funds", "/funds/export.xlsx", "/funds/approvals", "/receivables",
                 f"/receivables/{partner.id}", "/receivables/export.xlsx", "/payables",
                 f"/payables/{stocked['supplier'].id}"]:  # fmt: skip
        assert owner_client.get(path).status_code == 200, path
    assert "1,600" in owner_client.get("/receivables").text
    ledger = owner_client.get(f"/receivables/{partner.id}").text
    assert p.number in ledger and "1,600" in ledger
    # 전표 화면에 수금 금액이 보인다
    voucher_page = owner_client.get(f"/sales/{p.allocations[0].voucher_id}").text
    assert "남은 금액 1,600원" in voucher_page


def test_receipt_form_errors_keep_input(owner_client, db, stocked):
    token = _token(owner_client, "/funds/new")
    res = owner_client.post("/funds", data={
        "kind": "receipt", "payment_date": today().isoformat(),
        "partner_id": stocked["customer"].id, "amount": "0", "csrf_token": token})  # fmt: skip
    assert res.status_code == 400 and "0보다 커야" in res.text


def test_disbursement_approval_by_owner(client, db, business, stocked, make_user):
    business.payment_approval_threshold = 10000
    db.commit()
    login(client, stocked["user"])  # 경리
    token = _token(client, "/funds/new?kind=disbursement")
    client.post("/funds", data={
        "kind": "disbursement", "payment_date": today().isoformat(),
        "partner_id": stocked["supplier"].id, "amount": "66000", "csrf_token": token})  # fmt: skip
    p = db.scalar(select(Payment))
    assert p.status == PaymentStatus.PENDING
    assert "대표 승인 대기" in client.get("/funds/approvals").text

    client.get("/logout")
    owner = make_user(Role.OWNER, pin=True)
    login(client, owner)
    page = client.get("/funds/approvals").text
    assert re.search(r"지급 승인\s*1", page) and "승인</button>" in page
    token = _token(client, "/funds/approvals")
    # 간편 비밀번호가 없거나 틀리면 승인되지 않는다
    for bad in ("", "000000"):
        client.post(f"/funds/{p.id}/approve", data={"pin": bad, "csrf_token": token})
        db.refresh(p)
        assert p.status == PaymentStatus.PENDING
    client.post(f"/funds/{p.id}/approve", data={"pin": PIN, "csrf_token": token})
    db.refresh(p)
    assert p.status == PaymentStatus.CONFIRMED


def test_tax_invoice_screens(owner_client, db, stocked):
    assert "편의점" in owner_client.get("/tax-invoice").text  # 발행 대상
    assert owner_client.get("/tax-invoice/targets.xlsx").status_code == 200
    token = _token(owner_client, "/tax-invoice/upload")
    data = _xlsx([_row("A-1", today(), OUR, CUSTOMER, 6000, 600),
                  _row("A-2", today(), OUR, CUSTOMER, 1000, 100)])  # fmt: skip
    res = owner_client.post(
        "/tax-invoice/upload", data={"csrf_token": token},
        files={"file": ("hometax.xlsx", data, "application/octet-stream")},
    )  # fmt: skip
    assert res.status_code == 200 and "2건을 새로 올리고 1건을" in res.text
    assert "발행할 세금계산서가 없어요" in owner_client.get("/tax-invoice").text
    for path in ["/tax-invoice/list", "/tax-invoice/list?matched=0", "/tax-invoice/list.xlsx",
                 "/tax-invoice/summary", "/tax-invoice/summary?period=H2"]:  # fmt: skip
        assert owner_client.get(path).status_code == 200, path
    summary = owner_client.get("/tax-invoice/summary").text
    assert "7,000" in summary  # 공급가액 합계
    loose = db.scalar(select(TaxInvoice).where(TaxInvoice.approval_no == "A-2"))
    page = owner_client.get(f"/tax-invoice/{loose.id}").text
    assert "전표 골라 대사하기" in page
    matched = db.scalar(select(TaxInvoice).where(TaxInvoice.approval_no == "A-1"))
    token = _token(owner_client, f"/tax-invoice/{matched.id}")
    owner_client.post(f"/tax-invoice/{matched.id}/unmatch", data={"csrf_token": token})
    db.refresh(matched)
    assert matched.matched_at is None


def test_tax_agent_sees_only_closed_months(client, db, business, stocked, make_user):
    from app.services import tax_invoice as ti

    ti.import_file(db, business, stocked["user"],
                   _xlsx([_row("T-1", today(), OUR, CUSTOMER, 6000, 600)]), "a.xlsx")  # fmt: skip
    db.commit()
    login(client, make_user(Role.TAX_AGENT))
    assert client.get("/tax-invoice").headers["location"] == "/tax-invoice/list"
    assert "T-1" not in client.get("/tax-invoice/list").text
    from app.services import closing

    closing.close_month(db, business.id, closing.year_month(today()), None)
    db.commit()
    assert "T-1" in client.get("/tax-invoice/list").text


def test_accounts_tab_and_business_setting(owner_client, db, business):
    page = owner_client.get("/master/accounts").text
    assert "재고자산폐기손실" in page and "자동 분개" in page
    token = _token(owner_client, "/master/accounts")
    data = {"code": "812", "name": "여비교통비", "kind": "expense", "csrf_token": token}
    owner_client.post("/master/accounts", data=data)
    assert "여비교통비" in owner_client.get("/master/accounts").text
    token = _token(owner_client, "/settings")
    owner_client.post("/settings", data={
        "name": business.name, "tax_type": "general", "fiscal_end_month": 12,
        "payment_approval_threshold": 1000000, "disposal_to_cogs": "true",
        "csrf_token": token})  # fmt: skip
    db.refresh(business)
    assert business.disposal_to_cogs is True


def test_dashboard_with_data(owner_client, db, stocked):
    html = owner_client.get("/").text
    assert "처리 대기" in html and "30일 넘은 미수금" in html
    assert "6,600" in html  # 전체 미수금


def test_transfer_screens(owner_client, db, business, stocked):
    a = db.scalar(select(Warehouse).where(Warehouse.is_default))
    b = Warehouse(business_id=business.id, name="매장 창고")
    db.add(b)
    db.commit()
    assert owner_client.get("/inventory/transfers").status_code == 200
    sheet = owner_client.get(f"/inventory/transfers/new?src={a.id}&dst={b.id}").text
    assert "생수 500mL" in sheet and "190병" in sheet
    token = _token(owner_client, "/inventory/transfers")
    res = owner_client.post("/inventory/transfers", data={
        "transfer_date": today().isoformat(), "from_warehouse_id": a.id, "to_warehouse_id": b.id,
        "item_id": [stocked["water"].id], "qty": ["10"], "csrf_token": token})  # fmt: skip
    assert res.status_code == 303
    t = db.scalar(select(StockTransfer))
    page = owner_client.get(f"/inventory/transfers/{t.id}").text
    assert "기본 창고 → 매장 창고" in page and "이동 취소" in page
    moves = owner_client.get(f"/inventory/moves?warehouse={b.id}").text
    assert "창고 이동" in moves and t.number in moves
    stock_b = owner_client.get(f"/inventory?warehouse={b.id}&has_stock=1").text
    assert "10병 · 5L" in stock_b
    bad = owner_client.post("/inventory/transfers", data={
        "transfer_date": today().isoformat(), "from_warehouse_id": a.id, "to_warehouse_id": b.id,
        "item_id": [stocked["water"].id], "qty": ["999"], "csrf_token": token})  # fmt: skip
    assert bad.status_code == 400 and "가용 재고가" in bad.text and 'value="999"' in bad.text
