import pytest

from app import manage
from app.models import AuditLog, Role
from tests.conftest import csrf_of, login


def test_reset_pin_command(client, db, make_user, capsys):
    owner = make_user(Role.OWNER, pin=True)
    owner.pin_failed = 3
    db.commit()

    manage.main(["reset-pin", "--email", owner.email.upper()])
    assert "초기화했어요" in capsys.readouterr().out
    db.refresh(owner)
    assert owner.pin_hash is None and owner.pin_failed == 0
    assert db.query(AuditLog).filter_by(target_table="user_account", user_id=None).count()

    # 다음 로그인은 간편 비밀번호 없이 들어와 바로 새로 등록하게 된다
    login(client, owner)
    assert client.get("/").headers["location"] == "/account/pin"
    assert csrf_of(client, "/account/pin")

    with pytest.raises(SystemExit, match="찾을 수 없어요"):
        manage.main(["reset-pin", "--email", "nobody@baro.test"])
