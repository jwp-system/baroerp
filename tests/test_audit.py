import io

from openpyxl import load_workbook
from sqlalchemy import select

from app.models import AuditLog, User
from tests.conftest import csrf_of


def test_business_update_is_audited(owner_client, db):
    token = csrf_of(owner_client, "/settings")
    owner_client.post(
        "/settings",
        data={
            "name": "바로상사2",
            "tax_type": "general",
            "fiscal_end_month": "12",
            "payment_approval_threshold": "500000",
            "csrf_token": token,
        },
    )
    log = db.scalars(
        select(AuditLog).where(AuditLog.target_table == "business", AuditLog.action == "update")
    ).one()
    assert log.user_id == owner_client.user.id
    assert log.before == {"name": "바로상사", "payment_approval_threshold": 1_000_000}
    assert log.after == {"name": "바로상사2", "payment_approval_threshold": 500_000}


def test_user_create_is_audited_with_masked_secrets(owner_client, db):
    token = csrf_of(owner_client, "/settings/users")
    owner_client.post(
        "/settings/users",
        data={
            "name": "김경리",
            "email": "kim@baro.test",
            "role": "accountant",
            "password": "password123",
            "csrf_token": token,
        },
    )
    user = db.scalar(select(User).where(User.email == "kim@baro.test"))
    log = db.scalars(
        select(AuditLog).where(
            AuditLog.target_table == "user_account",
            AuditLog.action == "create",
            AuditLog.target_id == user.id,
        )
    ).one()
    assert log.after["password_hash"] == "***"
    assert log.after["role"] == "accountant"


def test_audit_list_filter_and_export(owner_client):
    token = csrf_of(owner_client, "/settings")
    owner_client.post(
        "/settings",
        data={
            "name": "바로상사2",
            "tax_type": "general",
            "fiscal_end_month": "12",
            "payment_approval_threshold": "1000000",
            "csrf_token": token,
        },
    )
    page = owner_client.get("/settings/audit?table=business")
    assert page.status_code == 200
    assert "바로상사2" in page.text

    partial = owner_client.get("/settings/audit?table=business", headers={"HX-Request": "true"})
    assert "<html" not in partial.text and "바로상사2" in partial.text

    res = owner_client.get("/settings/audit/export.xlsx?table=business")
    assert res.status_code == 200
    ws = load_workbook(io.BytesIO(res.content)).active
    rows = list(ws.values)
    assert rows[0][:3] == ("일시", "사용자", "작업")
    assert any(r[2] == "수정" for r in rows[1:])
