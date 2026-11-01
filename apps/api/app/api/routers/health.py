from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import DB

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: DB) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ready"}
