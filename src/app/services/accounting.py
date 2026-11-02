"""계정과목 기본값과 자동 분개. 계정 코드는 도입 시 세무대리인과 확정한다(기획안 5장)."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, Business, JournalEntry, JournalLine
from app.models.accounting import AccountKind, JournalSource

# (코드, 이름, 유형, 시스템 키)
DEFAULT_ACCOUNTS = [
    ("101", "현금", AccountKind.ASSET, "cash"),
    ("103", "보통예금", AccountKind.ASSET, "bank"),
    ("108", "외상매출금", AccountKind.ASSET, "receivable"),
    ("135", "부가세대급금", AccountKind.ASSET, "vat_paid"),
    ("146", "상품", AccountKind.ASSET, "inventory"),
    ("251", "외상매입금", AccountKind.LIABILITY, "payable"),
    ("255", "부가세예수금", AccountKind.LIABILITY, "vat_received"),
    ("401", "상품매출", AccountKind.REVENUE, "sales"),
    ("451", "상품매출원가", AccountKind.EXPENSE, "cogs"),
    ("958", "재고자산감모손실", AccountKind.EXPENSE, "shrinkage_loss"),
    ("959", "재고자산폐기손실", AccountKind.EXPENSE, "disposal_loss"),
]


class AccountingError(ValueError):
    pass


def seed_accounts(db: Session, business: Business) -> None:
    existing = set(db.scalars(select(Account.system_key).where(Account.business_id == business.id)))
    codes = set(db.scalars(select(Account.code).where(Account.business_id == business.id)))
    for code, name, kind, key in DEFAULT_ACCOUNTS:
        if key in existing:
            continue
        if code in codes:  # 사용자가 같은 코드를 이미 쓰고 있으면 뒤에 붙인다
            code = f"{code}{key[:3].upper()}"
        db.add(Account(business_id=business.id, code=code, name=name, kind=kind, system_key=key))


def account(db: Session, business_id: int, key: str) -> Account:
    acc = db.scalar(
        select(Account).where(Account.business_id == business_id, Account.system_key == key)
    )
    if acc is None:
        raise AccountingError(
            f"자동 분개에 쓰는 계정({key})이 없어요. 기준정보 > 계정과목을 확인해 주세요"
        )
    return acc


def disposal_loss_account(db: Session, business: Business) -> Account:
    return account(db, business.id, "cogs" if business.disposal_to_cogs else "disposal_loss")


def post(
    db: Session,
    business_id: int,
    entry_date: date,
    source: JournalSource,
    memo: str,
    lines: list[tuple[Account, Decimal, Decimal]],
) -> JournalEntry | None:
    """(계정, 차변, 대변) 목록으로 분개를 만든다. 금액이 모두 0이면 만들지 않는다."""
    lines = [(acc, dr, cr) for acc, dr, cr in lines if dr or cr]
    if not lines:
        return None
    if sum(dr for _, dr, _ in lines) != sum(cr for _, _, cr in lines):
        raise AccountingError("차변과 대변 합계가 달라요")
    entry = JournalEntry(business_id=business_id, entry_date=entry_date, source=source, memo=memo)
    for no, (acc, dr, cr) in enumerate(lines, start=1):
        entry.lines.append(
            JournalLine(business_id=business_id, line_no=no, account_id=acc.id, debit=dr, credit=cr)
        )
    db.add(entry)
    db.flush()
    return entry


def loss_lines(loss_acc: Account, asset_acc: Account, amount: Decimal):
    """손실 amount(양수) → (차) 손실 / (대) 자산. 음수면 반대(이익·취소)."""
    if amount >= 0:
        return [(loss_acc, amount, Decimal(0)), (asset_acc, Decimal(0), amount)]
    return [(asset_acc, -amount, Decimal(0)), (loss_acc, Decimal(0), -amount)]


def reverse(db: Session, entry: JournalEntry | None, entry_date: date, memo: str):
    """차·대를 바꾼 역분개"""
    if entry is None:
        return None
    rev = JournalEntry(
        business_id=entry.business_id, entry_date=entry_date, source=entry.source, memo=memo
    )
    for ln in entry.lines:
        rev.lines.append(
            JournalLine(
                business_id=entry.business_id,
                line_no=ln.line_no,
                account_id=ln.account_id,
                debit=ln.credit,
                credit=ln.debit,
            )
        )
    db.add(rev)
    db.flush()
    return rev
