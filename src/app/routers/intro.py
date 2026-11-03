"""데모 사이트 전용: 제품 소개 페이지와 로그인 없이 역할별로 둘러보기.
DEMO_MODE가 아니면 없는 주소처럼 동작한다(설치형·SaaS 고객 사이트에는 노출하지 않음)."""

from datetime import timedelta
from decimal import Decimal
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import auth, demo
from app.config import get_settings
from app.db import get_db
from app.models import Business, Disposal, Payment, User, Voucher
from app.models.finance import PaymentKind, PaymentStatus
from app.models.inventory import DisposalStatus
from app.models.user import ROLE_LABELS
from app.models.voucher import EFFECTIVE_STATUSES
from app.services import payments
from app.templating import flash, render, templates
from app.timeutil import today

router = APIRouter(include_in_schema=False)
CONTACT_URL = "https://jwp-system.duckdns.org/contact?type=erp"  # 도입 문의 게시판

# 앱 화면 위 데모 안내 줄과 로그인 화면의 '바로 들어가기'에서 쓴다
templates.env.globals["demo_accounts"] = [
    (key, name, ROLE_LABELS[role]) for key, (name, _, role) in demo.DEMO_ACCOUNTS.items()
]
templates.env.globals["demo_pin"] = demo.DEMO_PIN
templates.env.globals["demo_password"] = demo.DEMO_PASSWORD
DB = Annotated[Session, Depends(get_db)]

# 체험 카드: (계정 키, 제목, 열 화면, 설명)
TRIES = [
    ("owner", "이번 달 돈의 흐름", "/",
     "매출·매입·이익과 승인 대기, 30일 넘은 미수금을 한 화면에서"),
    ("owner", "지급 승인", "/funds/approvals",
     f"기준금액 이상 지급을 간편 비밀번호 {demo.DEMO_PIN}로 승인해 보세요"),
    ("owner", "거래처 상황판", "/partners",
     "거래처 하나의 매출·잔액·연체·자주 거래한 품목을 한눈에"),
    ("sales", "매출 전표 입력", "/sales/new",
     "품목을 고르면 매출 기본 단위와 단가가 채워지고 거래명세서까지"),
    ("sales", "미수금 에이징", "/receivables",
     "결제조건이 지난 금액을 거래처별·구간별로"),
    ("purchasing", "매입 전표 입력", "/purchase/new",
     "박스로 사서 병으로 판매, 재고 단위로 자동 환산"),
    ("purchasing", "재고 현황·폐기", "/inventory",
     "창고별 가용 재고, 파손 폐기 등록과 손실 분개"),
    ("accountant", "세금계산서 대사", "/tax-invoice/upload",
     "홈택스 엑셀을 올리면 전표와 자동으로 맞춰요(아래 샘플 파일)"),
    ("accountant", "보고서·월 마감", "/reports",
     "손익·부가세 자료, 마감한 달은 수정이 잠겨요"),
]  # fmt: skip


def _demo_only() -> None:
    if not get_settings().demo_mode:
        raise HTTPException(status_code=404)


def _stats(db: Session) -> dict | None:
    """히어로 화면에 보여 줄 데모 사업장의 실제 숫자"""
    business = db.scalar(select(Business).limit(1))
    if business is None:
        return None
    t = today()
    sales = db.scalar(
        select(func.sum(Voucher.supply_amount)).where(
            Voucher.business_id == business.id,
            Voucher.kind == "sales",
            Voucher.voucher_date >= t.replace(day=1),
            Voucher.voucher_date <= t,
            Voucher.status.in_(EFFECTIVE_STATUSES),
        )
    )
    receivables = payments.balances(db, business.id, PaymentKind.RECEIPT, t)
    return {
        "business": business,
        "sales": Decimal(sales or 0),
        "receivable": sum((b.balance for b in receivables), Decimal(0)),
        "overdue": sum(1 for b in receivables if b.overdue_30 > 0),
        "disposals": db.scalar(
            select(func.count()).where(
                Disposal.business_id == business.id, Disposal.status == DisposalStatus.PENDING
            )
        ),
        "pending": db.scalar(
            select(func.count()).where(
                Payment.business_id == business.id, Payment.status == PaymentStatus.PENDING
            )
        ),
    }


@router.get("/intro")
def intro(request: Request, db: DB):
    _demo_only()
    accounts = [
        {"key": key, "name": name, "email": email, "role": ROLE_LABELS[role]}
        for key, (name, email, role) in demo.DEMO_ACCOUNTS.items()
    ]
    last = today().replace(day=1) - timedelta(days=1)
    ctx = {
        "stats": _stats(db),
        "accounts": accounts,
        "account_names": {a["key"]: f"{a['role']} {a['name']}" for a in accounts},
        "password": demo.DEMO_PASSWORD,
        "pin": demo.DEMO_PIN,
        "tries": TRIES,
        "contact_url": CONTACT_URL,
        "last_month": f"{last.year}년 {last.month}월",
    }
    return render(request, "intro.html", ctx)


@router.get("/demo/enter/{key}")
def enter(request: Request, db: DB, key: str, next: str = "/"):
    """로그인 없이 데모 계정으로 바로 들어간다(공용 계정이라 간편 비밀번호 단계도 건너뜀)."""
    _demo_only()
    account = demo.DEMO_ACCOUNTS.get(key)
    if account is None:
        raise HTTPException(status_code=404)
    user = db.scalar(select(User).where(User.email == account[1]))
    if user is None or not user.is_active:
        flash(request, "데모 데이터를 준비하고 있어요. 잠시 뒤 다시 눌러 주세요", "error")
        return RedirectResponse("/intro", status_code=303)
    auth.login(request, db, user)
    # 같은 사이트 안의 주소로만 보낸다(외부 주소로 넘기는 오픈 리다이렉트 방지)
    if not next.startswith("/") or next.startswith("//") or "\\" in next:
        next = "/"
    return RedirectResponse(next, status_code=303)


@router.get("/intro/hometax-{direction}.xlsx")
def hometax_sample(db: DB, direction: str):
    """지난달 전표에 맞춘 홈택스 세금계산서 엑셀(체험용)"""
    _demo_only()
    business = db.scalar(select(Business).limit(1))
    if direction not in ("sales", "purchase") or business is None:
        raise HTTPException(status_code=404)
    data = demo.hometax_last_month_xlsx(db, business, direction)
    name = f"홈택스_{'매출' if direction == 'sales' else '매입'}_세금계산서_지난달.xlsx"
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )
