import pytest

from app.models import Role
from tests.conftest import csrf_of, login


@pytest.mark.parametrize(
    ("role", "allowed", "denied"),
    [
        (Role.ACCOUNTANT, "/funds", "/settings"),
        (Role.SALES, "/sales", "/purchase"),
        (Role.SALES, "/receivables", "/payables"),
        (Role.PURCHASING, "/purchase", "/sales"),
        (Role.PURCHASING, "/payables", "/receivables"),
        (Role.ACCOUNTANT, "/master/partners", "/settings"),
        (Role.SALES, "/receivables", "/master/items"),
        (Role.TAX_AGENT, "/tax-invoice/list", "/funds"),
        (Role.PURCHASING, "/inventory", "/funds"),
        (Role.SALES, "/sales", "/inventory"),
    ],
)
def test_default_menu_permissions(client, make_user, role, allowed, denied):
    login(client, make_user(role))
    assert client.get(allowed).status_code == 200
    assert client.get(denied).status_code == 403


def test_user_without_dashboard_redirected_to_first_menu(client, make_user):
    login(client, make_user(Role.SALES))
    res = client.get("/")
    assert res.headers["location"] == "/sales"


def test_sidebar_hides_denied_menus(client, make_user):
    login(client, make_user(Role.PURCHASING))
    html = client.get("/purchase").text
    assert 'href="/purchase" class="nav-item"' in html
    assert 'href="/sales" class="nav-item"' not in html


def test_business_can_override_permissions(owner_client, make_user, client):
    token = csrf_of(owner_client, "/settings/permissions")
    owner_client.post(
        "/settings/permissions",
        data={"sales:sales": "1", "sales:reports": "1", "csrf_token": token},
    )
    token = csrf_of(owner_client, "/")
    owner_client.post("/logout", data={"csrf_token": token})

    login(client, make_user(Role.SALES))
    assert client.get("/reports").status_code == 200
    assert client.get("/receivables").status_code == 403  # 기본값에선 허용, 조정으로 제외


def test_owner_cannot_demote_self(owner_client, db):
    owner = owner_client.user
    token = csrf_of(owner_client, "/settings/users")
    owner_client.post(
        f"/settings/users/{owner.id}",
        data={"role": "sales", "is_active": "true", "csrf_token": token},
    )
    db.refresh(owner)
    assert owner.role == Role.OWNER
