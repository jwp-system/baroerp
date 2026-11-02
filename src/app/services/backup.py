"""내장 백업·복원(설치형·데모). DB 종류(SQLite·PostgreSQL)와 무관한 zip 한 파일:

- manifest.json: 앱·DB 버전(alembic), 만든 시각, 테이블별 행 수, 개인정보 암호화 키 확인값
- tables/<테이블>.json: 행 목록(개인정보 컬럼은 암호문 그대로)
- attachments/<키>: 증빙 첨부 파일

복원은 한 트랜잭션에서 모든 테이블을 비우고 다시 채운다. 다중 사업장(SaaS)은 운영사 백업을 쓴다.
"""

import base64
import io
import json
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    Connection,
    Date,
    DateTime,
    Engine,
    LargeBinary,
    Numeric,
    column,
    func,
    inspect,
    select,
    table,
    text,
)
from sqlalchemy.types import TypeDecorator

from app.config import get_settings
from app.models import Attachment, Base
from app.models.types import _fernet
from app.services import storage
from app.timeutil import now_local

FORMAT = 1
PREFIX = "baroerp-"
KEY_CHECK = "baroerp"


class BackupError(ValueError):
    pass


def _raw_table(t):
    """암호화 컬럼(TypeDecorator)은 저장된 값(암호문) 그대로 읽고 쓴다."""
    cols = [
        column(c.name, c.type.impl_instance if isinstance(c.type, TypeDecorator) else c.type)
        for c in t.columns
    ]
    return table(t.name, *cols)


def _encode(value):
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    return value


def _utc_datetime(value: str) -> datetime:
    # SQLite는 시간대 없이 UTC로 저장하므로, 시간대가 없으면 UTC로 본다
    d = datetime.fromisoformat(value)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _decoder(col_type):
    if isinstance(col_type, DateTime):
        return _utc_datetime
    if isinstance(col_type, Date):
        return date.fromisoformat
    if isinstance(col_type, Numeric):
        return Decimal
    if isinstance(col_type, LargeBinary):
        return base64.b64decode
    return None


def db_revision(conn: Connection) -> str | None:
    if not inspect(conn).has_table("alembic_version"):  # 테스트처럼 alembic 없이 만든 DB
        return None
    return conn.scalar(text("SELECT version_num FROM alembic_version"))


def create(engine: Engine) -> tuple[str, bytes]:
    buf = io.BytesIO()
    counts: dict[str, int] = {}
    keys: list[str] = []
    with engine.connect() as conn, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        revision = db_revision(conn)
        for t in Base.metadata.sorted_tables:
            raw = _raw_table(t)
            pk = t.primary_key.columns.values()[0].name
            rows = [
                {k: _encode(v) for k, v in row._mapping.items()}
                for row in conn.execute(select(raw).order_by(raw.c[pk]))
            ]
            counts[t.name] = len(rows)
            zf.writestr(f"tables/{t.name}.json", json.dumps(rows, ensure_ascii=False))
        keys = list(conn.scalars(select(Attachment.storage_key)))
        attached = 0
        for key in keys:
            data = storage.read(key)
            if data is not None:
                zf.writestr(f"attachments/{key}", data)
                attached += 1
        created = now_local()
        manifest = {
            "app": "baroerp",
            "format": FORMAT,
            "revision": revision,
            "created_at": created.isoformat(),
            "tables": counts,
            "attachments": attached,
            "key_check": _fernet().encrypt(KEY_CHECK.encode()).decode(),
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return f"{PREFIX}{created:%Y%m%d-%H%M%S}.zip", buf.getvalue()


def read_manifest(data: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BackupError("바로ERP 백업 파일이 아니에요") from exc
    if manifest.get("app") != "baroerp" or manifest.get("format") != FORMAT:
        raise BackupError("바로ERP 백업 파일이 아니에요")
    return manifest


def check(engine: Engine, data: bytes) -> dict:
    """복원 전에 파일·암호화 키·버전을 확인한다."""
    manifest = read_manifest(data)
    try:
        _fernet().decrypt(manifest["key_check"].encode())
    except Exception as exc:
        raise BackupError(
            "개인정보 암호화 키(DATA_KEY)가 백업할 때와 달라요. 같은 키로 설정한 뒤 복원해 주세요"
        ) from exc
    with engine.connect() as conn:
        revision = db_revision(conn)
    if manifest["revision"] != revision:
        saved = manifest["revision"]
        raise BackupError(f"백업 버전({saved})이 지금 버전({revision})과 달라 복원할 수 없어요")
    if unknown := set(manifest["tables"]) - {t.name for t in Base.metadata.sorted_tables}:
        raise BackupError(f"알 수 없는 테이블이 있어요: {', '.join(sorted(unknown))}")
    return manifest


def restore(engine: Engine, data: bytes) -> dict:
    """백업으로 DB 전체를 바꾼다. 실패하면 아무것도 바뀌지 않는다."""
    manifest = check(engine, data)
    tables = Base.metadata.sorted_tables
    with zipfile.ZipFile(io.BytesIO(data)) as zf, engine.begin() as conn:
        for t in reversed(tables):
            conn.execute(t.delete())
        for t in tables:
            if t.name not in manifest["tables"]:
                continue
            raw = _raw_table(t)
            decoders = {c.name: _decoder(c.type) for c in raw.columns}
            rows = json.loads(zf.read(f"tables/{t.name}.json"))
            for row in rows:
                for k, v in row.items():
                    if v is not None and decoders.get(k):
                        row[k] = decoders[k](v)
            if rows:
                conn.execute(raw.insert(), rows)
        if conn.dialect.name == "postgresql":
            _reset_sequences(conn, tables)
        store = storage.get_store(storage.ATTACHMENTS)
        for name in zf.namelist():
            if name.startswith("attachments/") and not name.endswith("/"):
                store.put(name.removeprefix("attachments/"), zf.read(name))
    return manifest


def _reset_sequences(conn: Connection, tables) -> None:
    for t in tables:
        if "id" not in t.c:
            continue
        max_id = conn.scalar(select(func.max(t.c.id)))
        seq = conn.scalar(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": t.name})
        if seq:
            conn.execute(
                text("SELECT setval(:s, :v, :called)"),
                {"s": seq, "v": max_id or 1, "called": max_id is not None},
            )


# ---------- 보관 ----------


def store_backup(name: str, data: bytes) -> None:
    storage.get_store(storage.BACKUPS).put(name, data, "application/zip")


def list_backups() -> list[storage.StoredFile]:
    files = [f for f in storage.get_store(storage.BACKUPS).list() if f.name.startswith(PREFIX)]
    return sorted(files, key=lambda f: f.name, reverse=True)


def read_backup(name: str) -> bytes | None:
    if not name.startswith(PREFIX) or "/" in name or not name.endswith(".zip"):
        return None
    return storage.get_store(storage.BACKUPS).get(name)


def prune(keep: int | None = None) -> None:
    """자동·수동 백업은 최근 N개만 남긴다(복원 직전 백업은 따로 N개)."""
    keep = keep or get_settings().backup_keep
    store = storage.get_store(storage.BACKUPS)
    files = list_backups()
    for before_restore in (False, True):
        group = [f for f in files if f.name.endswith("-before-restore.zip") == before_restore]
        for f in group[keep:]:
            store.delete(f.name)


def run_scheduled(engine: Engine) -> str:
    name, data = create(engine)
    store_backup(name, data)
    prune()
    return name
