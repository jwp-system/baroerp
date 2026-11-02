from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(db: Annotated[Session, Depends(get_db)]) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "deploy_mode": get_settings().deploy_mode}
