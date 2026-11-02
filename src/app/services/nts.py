"""사업자번호 검증과 국세청 사업자등록 상태조회(공공데이터포털 무료 API)."""

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Partner
from app.models.master import BizStatus

log = logging.getLogger(__name__)

STATUS_URL = "https://api.odcloud.kr/api/nts-businessman/v1/status"
BATCH = 100  # API 1회 최대 조회 건수
_CODE_MAP = {"01": BizStatus.ACTIVE, "02": BizStatus.SUSPENDED, "03": BizStatus.CLOSED}
_WEIGHTS = (1, 3, 7, 1, 3, 7, 1, 3, 5)


def digits(value: str | None) -> str:
    return "".join(ch for ch in value or "" if ch.isdigit())


def biz_no_error(biz_no: str) -> str | None:
    """사업자등록번호 오류 문구. 자리 수와 검증번호를 따로 알려 준다(데모 모드는 자리 수만 확인)."""
    if len(biz_no) != 10 or not biz_no.isdigit():
        return "사업자번호는 숫자 10자리로 입력해 주세요"
    if get_settings().demo_mode:
        return None
    d = [int(c) for c in biz_no]
    total = sum(x * w for x, w in zip(d[:9], _WEIGHTS, strict=True)) + (d[8] * 5) // 10
    if (10 - total % 10) % 10 != d[9]:
        return "없는 사업자번호예요. 번호를 다시 확인해 주세요"
    return None


def fetch_statuses(biz_nos: list[str]) -> dict[str, BizStatus]:
    """{사업자번호: 상태}. 서비스키가 없거나 호출이 실패하면 빈 dict."""
    key = get_settings().nts_api_key
    if not key or not biz_nos:
        return {}
    result: dict[str, BizStatus] = {}
    try:
        with httpx.Client(timeout=5) as client:
            for i in range(0, len(biz_nos), BATCH):
                res = client.post(
                    STATUS_URL,
                    params={"serviceKey": key, "returnType": "JSON"},
                    json={"b_no": biz_nos[i : i + BATCH]},
                )
                res.raise_for_status()
                for row in res.json().get("data", []):
                    result[row["b_no"]] = _CODE_MAP.get(
                        row.get("b_stt_cd", ""), BizStatus.UNREGISTERED
                    )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("사업자 상태조회 실패: %s", exc)
    return result


def refresh_partner_statuses(business_id: int, partner_ids: list[int]) -> None:
    """백그라운드 작업: 거래처 사업자 상태를 조회해 저장한다."""
    with SessionLocal() as db:
        partners = db.scalars(
            select(Partner).where(
                Partner.business_id == business_id,
                Partner.id.in_(partner_ids),
                Partner.biz_no.is_not(None),
            )
        ).all()
        statuses = fetch_statuses([p.biz_no for p in partners])
        if not statuses:
            return
        now = datetime.now(UTC)
        for p in partners:
            if p.biz_no in statuses:
                p.biz_status = statuses[p.biz_no]
                p.biz_status_checked_at = now
        db.commit()
