import os
import re
import tempfile

_db_dir = tempfile.mkdtemp(prefix="baroerp-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_dir}/test.db"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DEBUG"] = "true"
os.environ["DEMO_MODE"] = "false"
os.environ["STORAGE_LOCAL_DIR"] = f"{_db_dir}/attachments"
os.environ["BACKUP_LOCAL_DIR"] = f"{_db_dir}/backups"

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    Business,
    Item,
    ItemGroup,
    ItemUnit,
    Partner,
    Role,
    Unit,
    User,
)
from app.security import hash_password  # noqa: E402
from app.services.master import seed_business_defaults  # noqa: E402

PASSWORD = "password123"
PIN = "135790"


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


def csrf_of(client: TestClient, path: str) -> str:
    html = client.get(path).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def business(db) -> Business:
    biz = Business(biz_no="1234567815", name="바로상사")
    db.add(biz)
    db.flush()
    seed_business_defaults(db, biz)
    db.commit()
    return biz


@pytest.fixture
def make_user(db, business):
    def _make(role: Role, email: str | None = None, pin: bool = False) -> User:
        user = User(
            business_id=business.id,
            email=email or f"{role.value}@baro.test",
            name=role.value,
            password_hash=hash_password(PASSWORD),
            role=role,
            pin_hash=hash_password(PIN) if pin else None,
        )
        db.add(user)
        db.commit()
        return user

    return _make


def login(client: TestClient, user: User) -> None:
    token = csrf_of(client, "/login")
    res = client.post(
        "/login", data={"email": user.email, "password": PASSWORD, "csrf_token": token}
    )
    assert res.status_code == 303
    if res.headers["location"] == "/login/pin":
        token = csrf_of(client, "/login/pin")  # 로그인 단계마다 세션이 새로 시작된다
        res = client.post("/login/pin", data={"pin": PIN, "csrf_token": token})
        assert res.status_code == 303


@pytest.fixture
def owner_client(client, make_user):
    owner = make_user(Role.OWNER, pin=True)
    login(client, owner)
    client.user = owner
    return client


# 거래 테스트 공통: 생수(1박스 = 20병, 1병 = 500mL)·쌀(면세, kg), 매입처·매출처·공통 거래처
@pytest.fixture
def setup(db, business, make_user):
    units = {u.name: u for u in db.scalars(select(Unit))}
    uncat = db.scalar(select(ItemGroup).where(ItemGroup.is_uncategorized))
    water = Item(
        business_id=business.id, code="W-500", code_key="W-500", name="생수 500mL",
        group_id=uncat.id, stock_unit_id=units["병"].id, content_qty=Decimal(500),
        content_unit_id=units["mL"].id, purchase_price=Decimal(300), sale_price=Decimal(600),
    )  # fmt: skip
    water.units.append(ItemUnit(business_id=business.id, unit_id=units["박스"].id, factor=20))
    rice = Item(
        business_id=business.id, code="R-1", code_key="R-1", name="쌀", group_id=uncat.id,
        stock_unit_id=units["kg"].id, tax_exempt=True, sale_price=Decimal(3000),
    )  # fmt: skip
    supplier = Partner(business_id=business.id, name="물공장", kind="purchase")
    customer = Partner(business_id=business.id, name="편의점", kind="sales")
    both = Partner(business_id=business.id, name="도매상", kind="both")
    db.add_all([water, rice, supplier, customer, both])
    db.commit()
    user = make_user(Role.ACCOUNTANT)
    return {"units": units, "water": water, "rice": rice, "supplier": supplier,
            "customer": customer, "both": both, "user": user}  # fmt: skip
