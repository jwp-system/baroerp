"""홈택스 세금계산서 엑셀 업로드·자동 대사. 데모 시나리오 6번."""

import io
from datetime import timedelta
from decimal import Decimal

import pytest
from openpyxl import Workbook

from app.models import TaxInvoice
from app.models.finance import InvoiceDirection
from app.services import tax_invoice as ti
from app.services.voucher import VoucherError, cancel_voucher
from app.timeutil import today
from tests.test_vouchers import _buy, _sell

OUR = "1234567815"
CUSTOMER = "2208162517"
SUPPLIER = "1048145786"

# 홈택스 목록조회 엑셀처럼 제목 행 + 같은 이름의 '상호' 열 두 개
HEADER = ["작성일자", "승인번호", "발급일자", "공급자사업자등록번호", "종사업장번호", "상호",
          "대표자명", "공급받는자사업자등록번호", "종사업장번호", "상호", "대표자명", "합계금액",
          "공급가액", "세액", "품목명"]  # fmt: skip


def _xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["매출 전자(세금)계산서 목록"])
    ws.append([])
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _row(no, d, supplier, buyer, supply, vat, item="생수 500mL"):
    return [d.isoformat(), no, d.isoformat(), supplier, "", "공급자", "", buyer, "", "받는자",
            "", supply + vat, supply, vat, item]  # fmt: skip


@pytest.fixture
def parties(db, setup):
    setup["customer"].biz_no = CUSTOMER
    setup["supplier"].biz_no = SUPPLIER
    db.commit()
    return setup


def test_upload_matches_sales_and_purchase(db, business, parties):
    buy = _buy(db, business, parties)  # 60,000 + 6,000
    sale = _sell(db, business, parties, 10)  # 6,000 + 600
    data = _xlsx([
        _row("20261102-1", today(), OUR, CUSTOMER, 6000, 600),
        _row("20261102-2", today(), SUPPLIER, OUR, 60000, 6000),
        _row("20261102-3", today(), OUR, CUSTOMER, 9999, 999),  # 맞는 전표 없음
        _row("20261102-4", today(), "1111111111", "2222222222", 1, 0),
    ])  # fmt: skip
    log = ti.import_file(db, business, parties["user"], data, "hometax.xlsx")
    db.commit()
    assert (log.created, log.matched, log.duplicates) == (3, 2, 0)
    assert log.errors == [[7, "공급자·공급받는자 어디에도 우리 사업자번호가 없어요"]]
    inv_sale = db.query(TaxInvoice).filter_by(approval_no="20261102-1").one()
    assert inv_sale.direction == InvoiceDirection.SALES and inv_sale.counterparty_name == "받는자"
    assert sale.tax_invoice_id == inv_sale.id
    assert buy.tax_invoice_id is not None
    assert ti.issue_targets(db, business.id) == []

    again = ti.import_file(db, business, parties["user"], data, "hometax.xlsx")
    assert (again.created, again.duplicates) == (0, 3)


def test_monthly_summary_invoice_matches_all_vouchers(db, business, parties):
    _buy(db, business, parties)
    a = _sell(db, business, parties, 10, d=today().replace(day=1))
    b = _sell(db, business, parties, 20, d=today().replace(day=1))
    data = _xlsx([_row("M-1", today(), OUR, CUSTOMER, 18000, 1800)])
    log = ti.import_file(db, business, parties["user"], data, "monthly.xlsx")
    assert log.matched == 1
    assert a.tax_invoice_id == b.tax_invoice_id is not None


def test_manual_match_requires_same_amount_and_unmatch(db, business, parties):
    _buy(db, business, parties)
    sale = _sell(db, business, parties, 10, d=today() - timedelta(days=40))
    ti.import_file(
        db,
        business,
        parties["user"],
        _xlsx([_row("X-1", today(), OUR, CUSTOMER, 6000, 600)]),
        "a.xlsx",
    )  # noqa: E501
    inv = db.query(TaxInvoice).one()
    assert inv.matched_at is None  # 다른 달이라 자동 대사 안 됨
    ti.match_manual(db, business, inv, [sale.id])
    db.commit()
    assert sale.tax_invoice_id == inv.id and inv.match_method == "manual"
    with pytest.raises(VoucherError, match="세금계산서와 대사된"):
        cancel_voucher(db, business, parties["user"], sale)
    db.rollback()
    ti.unmatch(db, inv)
    db.commit()
    assert sale.tax_invoice_id is None

    other = _sell(db, business, parties, 20)
    with pytest.raises(ti.InvoiceError, match="금액이 달라요"):
        ti.match_manual(db, business, inv, [other.id])


def test_unreadable_files():
    with pytest.raises(ti.InvoiceError, match="xlsx"):
        ti.parse(b"", "old.xls")
    with pytest.raises(ti.InvoiceError, match="승인번호"):
        ti.parse(b"a,b\n1,2\n", "x.csv")


def test_csv_cp949():
    text = (
        ",".join(HEADER)
        + "\n"
        + ",".join(str(v) for v in _row("C-1", today(), OUR, CUSTOMER, 100, 10))
    )  # noqa: E501
    rows, errors = ti.parse(text.encode("cp949"), "list.csv")
    assert not errors and rows[0].supply == Decimal(100) and rows[0].buyer_name == "받는자"


def test_issue_deadline():
    from datetime import date

    assert ti.issue_deadline(date(2026, 11, 30)) == date(2026, 12, 10)
    assert ti.issue_deadline(date(2026, 12, 5)) == date(2027, 1, 10)
