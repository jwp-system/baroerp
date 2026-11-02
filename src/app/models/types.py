import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    key = (
        settings.data_key
        or base64.urlsafe_b64encode(
            hashlib.sha256(f"baroerp-data:{settings.secret_key}".encode()).digest()
        ).decode()
    )
    return Fernet(key)


class EncryptedString(TypeDecorator):
    """개인정보 컬럼 암호화 저장(개인정보보호법). 암호문이라 검색·정렬은 할 수 없다."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return None
        return _fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return _fernet().decrypt(value.encode()).decode()
