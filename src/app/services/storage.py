"""파일 저장소: 증빙 첨부와 백업 파일. 설치형은 로컬 디스크, 데모는 Cloud Storage(같은 인터페이스).

Cloud Storage는 JSON API를 httpx로 직접 부른다. Cloud Run에서는 메타데이터 서버가 주는
서비스 계정 토큰을 쓰므로 별도 라이브러리·키 파일이 필요 없다.
"""

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import StorageBackend, get_settings

MAX_BYTES = 10 * 1024 * 1024
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
}

ATTACHMENTS = "attachments"
BACKUPS = "backups"


class StorageError(ValueError):
    pass


@dataclass
class StoredFile:
    name: str
    size: int
    updated: datetime


class LocalStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise StorageError("잘못된 파일 경로예요")
        return path

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list(self) -> list[StoredFile]:
        if not self.root.is_dir():
            return []
        return [
            StoredFile(p.name, p.stat().st_size, datetime.fromtimestamp(p.stat().st_mtime, UTC))
            for p in self.root.iterdir()
            if p.is_file()
        ]


_token: tuple[str, float] | None = None
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
GCS = "https://storage.googleapis.com"


def _gcs_token() -> str:
    global _token
    if _token and _token[1] > time.time() + 60:
        return _token[0]
    res = httpx.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5)
    res.raise_for_status()
    body = res.json()
    _token = (body["access_token"], time.time() + body["expires_in"])
    return _token[0]


_client: httpx.Client | None = None


def _shared_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=60)
    return _client


class GcsStore:
    """버킷 안의 접두어(attachments/, backups/) 하나를 저장소 하나로 쓴다."""

    def __init__(self, bucket: str, prefix: str, client: httpx.Client | None = None):
        if not bucket:
            raise StorageError("STORAGE_BUCKET을 설정해 주세요")
        self.bucket, self.prefix = bucket, prefix.strip("/") + "/"
        self.client = client or _shared_client()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {_gcs_token()}"}

    def _object_url(self, key: str) -> str:
        return f"{GCS}/storage/v1/b/{self.bucket}/o/{quote(self.prefix + key, safe='')}"

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        res = self.client.post(
            f"{GCS}/upload/storage/v1/b/{self.bucket}/o",
            params={"uploadType": "media", "name": self.prefix + key},
            content=data,
            headers={**self._headers(), "Content-Type": content_type or "application/octet-stream"},
        )
        res.raise_for_status()

    def get(self, key: str) -> bytes | None:
        url = self._object_url(key)
        res = self.client.get(url, params={"alt": "media"}, headers=self._headers())
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.content

    def delete(self, key: str) -> None:
        res = self.client.delete(self._object_url(key), headers=self._headers())
        if res.status_code != 404:
            res.raise_for_status()

    def list(self) -> list[StoredFile]:
        files, page = [], None
        while True:
            params = {"prefix": self.prefix, "delimiter": "/"}
            if page:
                params["pageToken"] = page
            res = self.client.get(
                f"{GCS}/storage/v1/b/{self.bucket}/o", params=params, headers=self._headers()
            )
            res.raise_for_status()
            body = res.json()
            files += [
                StoredFile(
                    o["name"].removeprefix(self.prefix),
                    int(o["size"]),
                    datetime.fromisoformat(o["updated"].replace("Z", "+00:00")),
                )
                for o in body.get("items", [])
            ]
            page = body.get("nextPageToken")
            if not page:
                return files


def get_store(kind: str) -> LocalStore | GcsStore:
    settings = get_settings()
    if settings.storage_backend is StorageBackend.GCS:
        return GcsStore(settings.storage_bucket, kind)
    if settings.storage_backend is StorageBackend.LOCAL:
        return LocalStore(
            settings.storage_local_dir if kind == ATTACHMENTS else settings.backup_local_dir
        )
    raise StorageError(f"저장소({settings.storage_backend})가 아직 연결되지 않았어요")


# ---------- 증빙 첨부 ----------


def save(business_id: int, data: bytes, content_type: str) -> str:
    if content_type not in ALLOWED_TYPES:
        raise StorageError("사진(JPG·PNG·WEBP·HEIC)이나 PDF만 올릴 수 있어요")
    if len(data) > MAX_BYTES:
        raise StorageError("파일은 10MB까지 올릴 수 있어요")
    key = f"{business_id}/{uuid.uuid4().hex}{ALLOWED_TYPES[content_type]}"
    get_store(ATTACHMENTS).put(key, data, content_type)
    return key


def read(key: str) -> bytes | None:
    return get_store(ATTACHMENTS).get(key)
