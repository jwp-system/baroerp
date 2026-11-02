import base64
import hashlib
import hmac
import secrets

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    expected = base64.b64decode(digest)
    actual = hashlib.scrypt(
        password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p)
    )
    return hmac.compare_digest(actual, expected)


PIN_LENGTH = 6


def pin_format_error(pin: str) -> str | None:
    """간편 비밀번호: 숫자 6자리. 같은 숫자 반복·연속 숫자는 쉽게 맞힐 수 있어 막는다."""
    if len(pin) != PIN_LENGTH or not (pin.isascii() and pin.isdigit()):
        return f"숫자 {PIN_LENGTH}자리로 입력해 주세요"
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        return "같은 숫자나 연속된 숫자는 쓸 수 없어요"
    return None
