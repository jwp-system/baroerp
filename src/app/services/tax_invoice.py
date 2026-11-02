"""홈택스 세금계산서 엑셀 업로드와 전표 대사(기획안 6장).

홈택스 '목록조회' 엑셀은 앞쪽에 제목 행이 있고, 공급자·공급받는자 '상호' 열 이름이 같다.
그래서 '승인번호'가 있는 행을 머리글로 찾고, 상호는 사업자번호 열 뒤의 첫 '상호'로 읽는다.
"""

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Business, Partner, TaxInvoice, UploadLog, User, Voucher
from app.models.finance import InvoiceDirection
from app.models.voucher import VoucherKind, VoucherStatus
from app.services.nts import digits

HEADER_SCAN_ROWS = 20
MAX_ROWS = 5000
_VOUCHER_KIND = {
    InvoiceDirection.SALES: VoucherKind.SALES,
    InvoiceDirection.PURCHASE: VoucherKind.PURCHASE,
}


class InvoiceError(ValueError):
    pass


def _norm(h) -> str:
    return re.sub(r"[\s()]", "", str(h or ""))


def _rows_from_file(data: bytes, filename: str) -> list[list]:
    name = filename.lower()
    if name.endswith(".csv"):
        for enc in ("utf-8-sig", "cp949"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise InvoiceError("CSV 파일의 글자 인코딩을 알 수 없어요")
        return list(csv.reader(io.StringIO(text)))
    if name.endswith(".xls"):
        raise InvoiceError("예전 엑셀(.xls)은 읽을 수 없어요. 엑셀에서 .xlsx로 저장해 올려 주세요")
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl은 다양한 예외를 던진다
        raise InvoiceError("엑셀(.xlsx) 파일을 열 수 없어요") from exc
    rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    wb.close()
    return rows


@dataclass
class _Columns:
    approval_no: int
    issue_date: int
    supplier_no: int
    buyer_no: int
    supplier_name: int | None
    buyer_name: int | None
    supply: int
    vat: int
    total: int | None
    item: int | None


def _find_columns(header: list[str]) -> _Columns | None:
    def idx(*names, after=-1, before=None):
        for i, h in enumerate(header):
            if i > after and (before is None or i < before) and h in names:
                return i
        return None

    need = {
        "approval_no": idx("승인번호"),
        "issue_date": idx("작성일자"),
        "supplier_no": idx("공급자사업자등록번호"),
        "buyer_no": idx("공급받는자사업자등록번호"),
        "supply": idx("공급가액"),
        "vat": idx("세액"),
    }
    if any(v is None for v in need.values()):
        return None
    return _Columns(
        **need,
        supplier_name=idx("공급자상호")
        or idx("상호", after=need["supplier_no"], before=need["buyer_no"]),
        buyer_name=idx("공급받는자상호") or idx("상호", after=need["buyer_no"]),
        total=idx("합계금액"),
        item=idx("품목명"),
    )


def _to_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    d = digits(str(v))
    if len(d) != 8:
        raise InvoiceError("작성일자를 읽을 수 없어요")
    return date(int(d[:4]), int(d[4:6]), int(d[6:]))


def _to_money(v, label) -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else 0).replace(",", "").strip())
    except InvalidOperation as exc:
        raise InvoiceError(f"{label}을(를) 읽을 수 없어요") from exc


@dataclass
class ParsedRow:
    line: int
    approval_no: str
    issue_date: date
    supplier_no: str
    buyer_no: str
    supplier_name: str | None
    buyer_name: str | None
    supply: Decimal
    vat: Decimal
    total: Decimal
    item: str | None


def parse(data: bytes, filename: str) -> tuple[list[ParsedRow], list[tuple[int, str]]]:
    rows = _rows_from_file(data, filename)
    cols, start = None, 0
    for i, r in enumerate(rows[:HEADER_SCAN_ROWS]):
        cols = _find_columns([_norm(h) for h in r])
        if cols:
            start = i + 1
            break
    if cols is None:
        raise InvoiceError(
            "승인번호·작성일자·사업자등록번호·공급가액·세액 열을 찾지 못했어요. "
            "홈택스 세금계산서 목록조회에서 내려받은 파일인지 확인해 주세요"
        )
    parsed, errors = [], []
    for line, r in enumerate(rows[start:], start=start + 1):
        if not r or all(v in (None, "") for v in r):
            continue

        def at(i, r=r):
            return r[i] if i is not None and i < len(r) else None

        approval = re.sub(r"\s", "", str(at(cols.approval_no) or ""))
        if not approval:
            continue  # 합계 행 등
        try:
            supply = _to_money(at(cols.supply), "공급가액")
            vat = _to_money(at(cols.vat), "세액")
            total = (
                _to_money(at(cols.total), "합계금액") if cols.total is not None else supply + vat
            )
            parsed.append(
                ParsedRow(
                    line=line,
                    approval_no=approval,
                    issue_date=_to_date(at(cols.issue_date)),
                    supplier_no=digits(str(at(cols.supplier_no) or "")),
                    buyer_no=digits(str(at(cols.buyer_no) or "")),
                    supplier_name=(str(at(cols.supplier_name) or "").strip() or None),
                    buyer_name=(str(at(cols.buyer_name) or "").strip() or None),
                    supply=supply,
                    vat=vat,
                    total=total,
                    item=(str(at(cols.item) or "").strip()[:200] or None),
                )
            )
        except InvoiceError as exc:
            errors.append((line, str(exc)))
        if len(parsed) > MAX_ROWS:
            raise InvoiceError(f"한 번에 {MAX_ROWS:,}행까지 올릴 수 있어요")
    return parsed, errors


def import_file(
    db: Session, business: Business, user: User, data: bytes, filename: str
) -> UploadLog:
    """올바른 행은 저장하고 실패 행은 이력에 남긴다. 저장 후 자동 대사."""
    log = UploadLog(business_id=business.id, source="hometax", filename=filename[:255],
                    created_by_id=user.id)  # fmt: skip
    try:
        rows, errors = parse(data, filename)
    except InvoiceError as exc:
        log.errors = [[0, str(exc)]]
        db.add(log)
        db.flush()
        return log
    existing = set(
        db.scalars(select(TaxInvoice.approval_no).where(TaxInvoice.business_id == business.id))
    )
    db.add(log)
    db.flush()
    created: list[TaxInvoice] = []
    for r in rows:
        if r.approval_no in existing:
            log.duplicates += 1
            continue
        if r.supplier_no == business.biz_no:
            direction, other_no, other_name = InvoiceDirection.SALES, r.buyer_no, r.buyer_name
        elif r.buyer_no == business.biz_no:
            direction, other_no, other_name = (
                InvoiceDirection.PURCHASE,
                r.supplier_no,
                r.supplier_name,
            )
        else:
            errors.append((r.line, "공급자·공급받는자 어디에도 우리 사업자번호가 없어요"))
            continue
        inv = TaxInvoice(
            business_id=business.id,
            direction=direction,
            approval_no=r.approval_no,
            issue_date=r.issue_date,
            counterparty_biz_no=other_no,
            counterparty_name=other_name,
            supply_amount=r.supply,
            vat_amount=r.vat,
            total_amount=r.total,
            item_name=r.item,
            upload_id=log.id,
        )
        db.add(inv)
        existing.add(r.approval_no)
        created.append(inv)
    db.flush()
    log.total_rows = len(rows) + len([e for e in errors if e[0]])
    log.created = len(created)
    log.matched = auto_match(db, business, created)
    log.errors = [list(e) for e in sorted(errors)] or None
    return log


# ---------- 대사 ----------


def unmatched_vouchers(db: Session, business_id: int, direction: InvoiceDirection, biz_no=None):
    stmt = (
        select(Voucher)
        .join(Partner, Partner.id == Voucher.partner_id)
        .where(
            Voucher.business_id == business_id,
            Voucher.kind == _VOUCHER_KIND[InvoiceDirection(direction)],
            Voucher.status == VoucherStatus.CONFIRMED,
            Voucher.reversal_of_id.is_(None),
            Voucher.tax_invoice_id.is_(None),
            Voucher.total_amount > 0,
        )
        .options(selectinload(Voucher.partner))
        .order_by(Voucher.voucher_date, Voucher.id)
    )
    if biz_no is not None:
        stmt = stmt.where(Partner.biz_no == biz_no)
    return list(db.scalars(stmt))


def _link(inv: TaxInvoice, vouchers: list[Voucher], method: str) -> None:
    for v in vouchers:
        v.tax_invoice_id = inv.id
    inv.matched_at = datetime.now(UTC)
    inv.match_method = method


def auto_match(db: Session, business: Business, invoices: list[TaxInvoice]) -> int:
    """1) 같은 거래처·같은 금액 전표 1건(같은 날 우선, 없으면 같은 달)
    2) 같은 달 대사 안 된 전표 전체 합계가 같으면 월합계 발행으로 보고 모두 연결"""
    matched = 0
    by_key: dict[tuple, list[Voucher]] = defaultdict(list)
    for inv in invoices:
        key = (inv.direction, inv.counterparty_biz_no)
        if key not in by_key:
            by_key[key] = unmatched_vouchers(
                db, business.id, inv.direction, inv.counterparty_biz_no
            )
    for inv in sorted(invoices, key=lambda i: (i.issue_date, i.approval_no)):
        if inv.matched_at:
            continue
        pool = by_key[(inv.direction, inv.counterparty_biz_no)]
        same_month = [
            v for v in pool
            if (v.voucher_date.year, v.voucher_date.month)
            == (inv.issue_date.year, inv.issue_date.month)
        ]  # fmt: skip
        exact = [
            v for v in same_month
            if (v.supply_amount, v.vat_amount) == (inv.supply_amount, inv.vat_amount)
        ]  # fmt: skip
        exact.sort(key=lambda v: (v.voucher_date != inv.issue_date, v.voucher_date, v.id))
        if exact:
            chosen = [exact[0]]
        elif (
            len(same_month) > 1
            and sum(v.supply_amount for v in same_month) == inv.supply_amount
            and sum(v.vat_amount for v in same_month) == inv.vat_amount
        ):
            chosen = same_month
        else:
            continue
        _link(inv, chosen, "auto")
        for v in chosen:
            pool.remove(v)
        matched += 1
    db.flush()
    return matched


def match_manual(db: Session, business: Business, inv: TaxInvoice, voucher_ids: list[int]) -> None:
    if inv.matched_at:
        raise InvoiceError("이미 대사된 세금계산서예요")
    pool = {v.id: v for v in unmatched_vouchers(db, business.id, inv.direction)}
    chosen = [pool[i] for i in voucher_ids if i in pool]
    if not chosen or len(chosen) != len(set(voucher_ids)):
        raise InvoiceError("연결할 전표를 골라 주세요")
    if any(v.partner.biz_no != inv.counterparty_biz_no for v in chosen):
        raise InvoiceError("세금계산서와 사업자번호가 다른 거래처의 전표예요")
    supply = sum(v.supply_amount for v in chosen)
    vat = sum(v.vat_amount for v in chosen)
    if (supply, vat) != (inv.supply_amount, inv.vat_amount):
        raise InvoiceError(
            f"금액이 달라요: 전표 공급가액 {supply:,.0f}원·세액 {vat:,.0f}원, "
            f"세금계산서 {inv.supply_amount:,.0f}원·{inv.vat_amount:,.0f}원"
        )
    _link(inv, chosen, "manual")
    db.flush()


def unmatch(db: Session, inv: TaxInvoice) -> None:
    for v in db.scalars(select(Voucher).where(Voucher.tax_invoice_id == inv.id)):
        v.tax_invoice_id = None
    inv.matched_at = None
    inv.match_method = None
    db.flush()


# ---------- 발행 대상 ----------


def issue_deadline(d: date) -> date:
    """발급 기한: 공급일이 속한 달의 다음 달 10일"""
    first_next = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_next.replace(day=10)


def issue_targets(db: Session, business_id: int) -> list[Voucher]:
    """사업자번호가 있는 거래처의 매출 전표 중 세금계산서가 대사되지 않은 것"""
    return [
        v for v in unmatched_vouchers(db, business_id, InvoiceDirection.SALES) if v.partner.biz_no
    ]
