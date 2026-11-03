"""데모 사이트: 제품 소개, 로그인 없이 역할별 둘러보기, 공용 계정 보호, 매일 처음 상태로."""

import pytest
from sqlalchemy import func, select

from app import demo
from app.config import get_settings
from app.models import Business, Disposal, Payment, TaxInvoice, User
from app.models.finance import PaymentStatus
from app.models.inventory import DisposalStatus
from app.security import verify_password
from tests.conftest import csrf_of


@pytest.fixture
def demo_site(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_mode", True)
    return demo.seed(db, months=2)


def test_demo_pages_hidden_outside_demo_mode(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_token", "secret")
    assert client.get("/intro").status_code == 404
    assert client.get("/demo/enter/owner").status_code == 404
    res = client.post("/tasks/demo-reset", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 404


def test_intro_is_the_front_door(client, demo_site):
    assert client.get("/").headers["location"] == "/intro"
    page = client.get("/intro").text
    for text in ("박사장", "demo1@example.com", "demo4@example.com", demo.DEMO_PASSWORD,
                 demo.DEMO_PIN, 'href="/demo/enter/sales?next=/sales/new"'):  # fmt: skip
        assert text in page
    assert "강부장" in client.get("/login").text  # 로그인 화면에서도 바로 들어가기
    assert page.count('href="https://jwp-system.duckdns.org/contact?type=erp"') >= 3  # 도입 문의


def test_favicon(client):
    for path in ("/favicon.ico", "/apple-touch-icon.png", "/static/img/favicon.svg",
                 "/static/manifest.webmanifest"):  # fmt: skip
        assert client.get(path).status_code == 200, path
    assert 'rel="icon"' in client.get("/setup").text


def test_enter_as_role_without_login(client, demo_site):
    res = client.get("/demo/enter/sales?next=/receivables")
    assert res.headers["location"] == "/receivables"
    page = client.get("/receivables").text
    assert "김과장(영업)로 보는 중" in page
    assert 'href="/demo/enter/owner"' in page  # 다른 역할로 바꾸기
    # 사이트 밖 주소로는 보내지 않는다
    assert client.get("/demo/enter/sales?next=//evil.example").headers["location"] == "/"
    assert client.get("/demo/enter/nobody").status_code == 404


def test_owner_tries_pin_approval(client, db, demo_site):
    p = db.scalar(select(Payment).where(Payment.status == PaymentStatus.PENDING))
    assert p is not None and p.created_by.name == "강부장"
    assert db.scalar(select(Disposal).where(Disposal.status == DisposalStatus.PENDING))
    client.get("/demo/enter/owner")
    token = csrf_of(client, "/funds/approvals")
    client.post(f"/funds/{p.id}/approve", data={"pin": demo.DEMO_PIN, "csrf_token": token})
    db.refresh(p)
    assert p.status == PaymentStatus.CONFIRMED


def test_shared_accounts_are_protected(client, db, demo_site):
    client.get("/demo/enter/owner")
    token = csrf_of(client, "/account")
    data = {"current": demo.DEMO_PASSWORD, "new": "changed-1234", "csrf_token": token}
    res = client.post("/account/password", data=data)
    assert res.status_code == 303
    owner = db.scalar(select(User).where(User.email == "demo1@example.com"))
    db.refresh(owner)
    assert verify_password(demo.DEMO_PASSWORD, owner.password_hash)
    assert "데모에서는 바꿀 수 없어요" in client.get("/account").text
    client.post("/settings/permissions", data={"csrf_token": token})
    business = db.get(Business, owner.business_id)
    db.refresh(business)
    assert business.menu_permissions is None


def test_hometax_sample_matches_last_month(client, db, demo_site):
    data = client.get("/intro/hometax-sales.xlsx").content
    client.get("/demo/enter/accountant")
    token = csrf_of(client, "/tax-invoice/upload")
    res = client.post("/tax-invoice/upload", data={"csrf_token": token},
                      files={"file": ("hometax.xlsx", data)})  # fmt: skip
    assert res.status_code == 200, res.text
    invoices = db.scalars(select(TaxInvoice)).all()
    assert invoices and all(i.matched_at for i in invoices)


def test_reset_restores_first_state(client, db, demo_site, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_token", "secret")
    db.scalar(select(User).where(User.email == "demo2@example.com")).is_active = False
    db.commit()
    res = client.post("/tasks/demo-reset", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 200
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Business)) == 1
    assert db.scalar(select(User).where(User.email == "demo2@example.com")).is_active
    assert client.get("/demo/enter/sales").headers["location"] == "/"
