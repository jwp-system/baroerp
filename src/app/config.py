from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class DeployMode(StrEnum):
    SAAS = "saas"  # 바로ERP 클라우드: 다중 사업장
    SINGLE = "single"  # 바로ERP 설치형·데모: 사업장 1개 고정


class StorageBackend(StrEnum):
    LOCAL = "local"
    GCS = "gcs"
    S3 = "s3"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "바로ERP"
    debug: bool = False
    # 데모: 사업자번호 검증번호(체크섬) 확인을 생략하고 10자리 숫자만 확인
    demo_mode: bool = False

    deploy_mode: DeployMode = DeployMode.SINGLE
    database_url: str = "sqlite:///./data/baroerp.db"
    db_pool_size: int = 5

    secret_key: str = "change-me"
    # 개인정보(거래처 담당자 연락처) 암호화 키. 비우면 secret_key에서 파생 — 운영에서는 따로 지정
    data_key: str = ""
    session_max_age: int = 60 * 60 * 8

    # Cloud Run 데모에서는 끄고 Cloud Scheduler로 대신한다
    scheduler_enabled: bool = True

    # 공공데이터포털 국세청 사업자등록 상태조회 API 서비스키(비우면 조회 생략)
    nts_api_key: str = ""

    storage_backend: StorageBackend = StorageBackend.LOCAL
    storage_local_dir: Path = Path("./data/attachments")
    storage_bucket: str = ""

    # 내장 백업: 매일 03:00(스케줄러) 또는 Cloud Scheduler가 /tasks/backup 호출. 최근 N개 보관
    backup_local_dir: Path = Path("./data/backups")
    backup_keep: int = 7
    # Cloud Scheduler 등 외부 예약 호출용 토큰(Authorization: Bearer). 비우면 /tasks 사용 안 함
    task_token: str = ""

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_saas(self) -> bool:
        return self.deploy_mode is DeployMode.SAAS


@lru_cache
def get_settings() -> Settings:
    return Settings()
