import io
import json
import shutil
import zipfile

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import engine
from app.models import Attachment, Item, Partner, Role
from app.services import backup, storage
from tests.conftest import PASSWORD, csrf_of, login


@pytest.fixture(autouse=True)
def _clean_backups():
    shutil.rmtree(get_settings().backup_local_dir, ignore_errors=True)
    yield


def _restore(client, **data):
    token = csrf_of(client, "/settings/backup")
    files = data.pop("files", None)
    return client.post("/settings/backup/restore", data={"csrf_token": token, **data}, files=files)


def test_backup_and_restore_round_trip(owner_client, db, setup, business):
    partner = setup["supplier"]
    partner.contact_phone = "010-1111-2222"
    key = storage.save(business.id, b"%PDF-1.4 proof", "application/pdf")
    db.add(Attachment(business_id=business.id, owner_type="disposal", owner_id=1,
                      filename="확인서.pdf", content_type="application/pdf", size=14,
                      storage_key=key))  # fmt: skip
    db.commit()

    token = csrf_of(owner_client, "/settings/backup")
    res = owner_client.post("/settings/backup", data={"csrf_token": token})
    assert res.status_code == 303
    name = backup.list_backups()[0].name
    page = owner_client.get("/settings/backup").text
    assert name in page
    zipped = owner_client.get(f"/settings/backup/files/{name}")
    assert zipped.status_code == 200
    manifest = backup.read_manifest(zipped.content)
    assert manifest["tables"]["item"] == 2 and manifest["attachments"] == 1
    # 개인정보는 암호문 그대로 들어간다
    rows = json.loads(zipfile.ZipFile(io.BytesIO(zipped.content)).read("tables/partner.json"))
    assert "010-1111-2222" not in json.dumps(rows)

    # 백업 후 바뀐 내용
    db.execute(Item.__table__.update().values(name="바뀜"))
    db.commit()
    storage.get_store(storage.ATTACHMENTS).delete(key)

    _restore(owner_client, name=name, password="wrong")
    assert db.scalar(select(Item.name).where(Item.code == "W-500")) == "바뀜"

    res = _restore(owner_client, name=name, password=PASSWORD)
    assert res.status_code == 303 and res.headers["location"] == "/login"
    db.expire_all()
    assert db.scalar(select(Item.name).where(Item.code == "W-500")) == "생수 500mL"
    assert db.get(Partner, partner.id).contact_phone == "010-1111-2222"
    assert storage.read(key) == b"%PDF-1.4 proof"
    names = [f.name for f in backup.list_backups()]
    assert any(n.endswith("-before-restore.zip") for n in names)
    # 복원 후에는 다시 로그인
    assert owner_client.get("/settings/backup").status_code == 303


def test_restore_rejects_wrong_password_and_bad_files(owner_client, db, business):
    _, data = backup.create(engine)
    res = _restore(owner_client, password="wrong", files={"file": ("b.zip", data)})
    assert res.headers["location"] == "/settings/backup"
    assert "비밀번호가 맞지 않아요" in owner_client.get("/settings/backup").text

    _restore(owner_client, password=PASSWORD, files={"file": ("x.zip", b"not a zip")})
    assert "바로ERP 백업 파일이 아니에요" in owner_client.get("/settings/backup").text

    # 다른 암호화 키로 만든 백업
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
        for n in src.namelist():
            body = src.read(n)
            if n == "manifest.json":
                m = json.loads(body)
                m["key_check"] = "gAAAAA-invalid"
                body = json.dumps(m).encode()
            dst.writestr(n, body)
    _restore(owner_client, password=PASSWORD, files={"file": ("k.zip", buf.getvalue())})
    assert "암호화 키" in owner_client.get("/settings/backup").text
    assert backup.list_backups() == []  # 검증에 실패하면 복원 직전 백업도 만들지 않는다


def test_backup_is_owner_only(client, make_user):
    login(client, make_user(Role.ACCOUNTANT))
    assert client.get("/settings/backup").status_code == 403


def test_prune_keeps_latest(business):
    for i in range(4):
        backup.store_backup(f"baroerp-2026010{i}-000000.zip", b"x")
        backup.store_backup(f"baroerp-2026010{i}-000000-before-restore.zip", b"x")
    backup.prune(keep=2)
    assert [f.name for f in backup.list_backups()] == [
        "baroerp-20260103-000000.zip",
        "baroerp-20260103-000000-before-restore.zip",
        "baroerp-20260102-000000.zip",
        "baroerp-20260102-000000-before-restore.zip",
    ]
    assert backup.read_backup("../etc/passwd") is None


def test_task_endpoint_needs_token(client, business, monkeypatch):
    assert client.post("/tasks/backup").status_code == 403
    monkeypatch.setattr(get_settings(), "task_token", "secret")
    assert client.post("/tasks/backup", headers={"Authorization": "Bearer nope"}).status_code == 403
    res = client.post("/tasks/backup", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 200 and res.json()["backup"].startswith("baroerp-")


def test_gcs_store_uses_json_api(monkeypatch):
    objects: dict[str, bytes] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer tok"
        path = req.url.path
        if req.method == "POST" and path == "/upload/storage/v1/b/bk/o":
            objects[req.url.params["name"]] = req.content
            return httpx.Response(200, json={})
        if req.method == "GET" and path == "/storage/v1/b/bk/o":
            prefix = req.url.params["prefix"]
            items = [{"name": n, "size": str(len(b)), "updated": "2026-11-02T00:00:00Z"}
                     for n, b in objects.items() if n.startswith(prefix)]  # fmt: skip
            return httpx.Response(200, json={"items": items})
        name = httpx.URL(str(req.url)).raw_path.decode().split("/o/")[1].split("?")[0]
        from urllib.parse import unquote

        name = unquote(name)
        if req.method == "GET":
            if name not in objects:
                return httpx.Response(404)
            return httpx.Response(200, content=objects[name])
        objects.pop(name, None)
        return httpx.Response(204)

    monkeypatch.setattr(storage, "_gcs_token", lambda: "tok")
    store = storage.GcsStore("bk", "backups", httpx.Client(transport=httpx.MockTransport(handler)))
    store.put("baroerp-1.zip", b"data")
    assert "backups/baroerp-1.zip" in objects
    assert store.get("baroerp-1.zip") == b"data"
    assert [f.name for f in store.list()] == ["baroerp-1.zip"]
    store.delete("baroerp-1.zip")
    assert store.get("baroerp-1.zip") is None
