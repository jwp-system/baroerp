import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _daily_backup() -> None:
    from app.db import engine
    from app.services import backup

    try:
        log.info("자동 백업: %s", backup.run_scheduled(engine))
    except Exception:
        log.exception("자동 백업 실패")


def start_scheduler() -> None:
    """내장 예약 작업(설치형). 데모(Cloud Run)는 끄고 Cloud Scheduler가 /tasks를 호출한다."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    if not get_settings().is_saas:  # SaaS는 운영사 DB 백업을 쓴다
        _scheduler.add_job(_daily_backup, "cron", hour=3, minute=0, id="daily_backup")
    _scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
