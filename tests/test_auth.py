from sqlalchemy import select

from app.models import Business, Role, User
from tests.conftest import PASSWORD, PIN, csrf_of, login


def test_first_visit_goes_to_setup(client):
    res = client.get("/")
    assert res.headers["location"] == "/setup"


def test_setup_creates_business_and_owner_then_requires_2fa(client, db):
    token = csrf_of(client, "/setup")
    res = client.post(
        "/setup",
        data={
            "biz_name": "바로상사",
            "biz_no": "123-45-67815",
            "tax_type": "general",
            "name": "박대표",
            "email": "Owner@Baro.test",
            "password": PASSWORD,
            "csrf_token": token,
        },
    )
    assert res.headers["location"] == "/account/pin"
    biz = db.scalar(select(Business))
    owner = db.scalar(select(User))
    assert biz.biz_no == "1234567815"
    assert owner.role == Role.OWNER and owner.email == "owner@baro.test"

    # 2단계 인증(간편 비밀번호) 등록 전에는 다른 화면으로 못 간다
    assert client.get("/").headers["location"] == "/account/pin"

    token = csrf_of(client, "/account/pin")
    for pin, confirm, msg in (
        ("12345", "12345", "숫자 6자리"),
        ("111111", "111111", "연속된 숫자"),
        ("123456", "123456", "연속된 숫자"),
        (PIN, "246801", "달라요"),
    ):
        res = client.post("/account/pin", data={"pin": pin, "pin_confirm": confirm,
                                                "csrf_token": token})  # fmt: skip
        assert res.status_code == 400 and msg in res.text
    res = client.post("/account/pin", data={"pin": PIN, "pin_confirm": PIN, "csrf_token": token})
    assert res.status_code == 303
    assert client.get("/").status_code == 200
    db.refresh(owner)
    assert owner.pin_hash and PIN not in owner.pin_hash

    # 사업장이 있으면 다시 설정할 수 없다
    assert client.get("/setup").headers["location"] == "/login"


def test_login_wrong_password(client, make_user):
    user = make_user(Role.ACCOUNTANT)
    token = csrf_of(client, "/login")
    res = client.post("/login", data={"email": user.email, "password": "x", "csrf_token": token})
    assert res.status_code == 400
    assert "맞지 않아요" in res.text


def _start_login(client, user):
    token = csrf_of(client, "/login")
    res = client.post(
        "/login", data={"email": user.email, "password": PASSWORD, "csrf_token": token}
    )
    assert res.headers["location"] == "/login/pin"
    return csrf_of(client, "/login/pin")


def test_login_with_pin(client, make_user):
    user = make_user(Role.OWNER, pin=True)
    token = _start_login(client, user)
    # 간편 비밀번호 전에는 로그인되지 않은 상태
    assert client.get("/").headers["location"] == "/login"

    bad = client.post("/login/pin", data={"pin": "000000", "csrf_token": token})
    assert bad.status_code == 400 and "4번 더" in bad.text
    ok = client.post("/login/pin", data={"pin": PIN, "csrf_token": token})
    assert ok.status_code == 303
    assert client.get("/").status_code == 200


def test_pin_locks_after_five_failures(client, db, make_user):
    user = make_user(Role.OWNER, pin=True)
    token = _start_login(client, user)
    for _ in range(5):
        res = client.post("/login/pin", data={"pin": "000000", "csrf_token": token})
    assert "15분 동안" in res.text
    # 잠긴 동안은 맞는 번호도 받지 않는다
    res = client.post("/login/pin", data={"pin": PIN, "csrf_token": token})
    assert res.status_code == 400 and "입력할 수 없어요" in res.text
    db.refresh(user)
    assert user.pin_locked_until is not None


def test_change_pin_requires_current(owner_client, db):
    token = csrf_of(owner_client, "/account/pin")
    data = {"pin": "246801", "pin_confirm": "246801", "csrf_token": token}
    res = owner_client.post("/account/pin", data={**data, "current": "000000"})
    assert res.status_code == 400 and "맞지 않아요" in res.text
    res = owner_client.post("/account/pin", data={**data, "current": PIN})
    assert res.status_code == 303
    db.refresh(owner_client.user)
    assert owner_client.user.pin_failed == 0


def test_inactive_user_cannot_login(client, db, make_user):
    user = make_user(Role.SALES)
    user.is_active = False
    db.commit()
    token = csrf_of(client, "/login")
    res = client.post(
        "/login", data={"email": user.email, "password": PASSWORD, "csrf_token": token}
    )
    assert res.status_code == 400


def test_logout(owner_client):
    token = csrf_of(owner_client, "/")
    owner_client.post("/logout", data={"csrf_token": token})
    assert owner_client.get("/").headers["location"] == "/login"


def test_post_without_csrf_is_rejected(client, make_user):
    user = make_user(Role.ACCOUNTANT)
    res = client.post("/login", data={"email": user.email, "password": PASSWORD})
    assert res.status_code == 403


def test_htmx_redirect_when_logged_out(client, business):
    res = client.get("/settings/audit", headers={"HX-Request": "true"})
    assert res.status_code == 204
    assert res.headers["HX-Redirect"] == "/login"


def test_change_password(client, make_user, db):
    user = make_user(Role.ACCOUNTANT)
    login(client, user)
    token = csrf_of(client, "/account")
    client.post(
        "/account/password",
        data={"current": PASSWORD, "new": "newpassword1", "csrf_token": token},
    )
    db.refresh(user)
    from app.security import verify_password

    assert verify_password("newpassword1", user.password_hash)
