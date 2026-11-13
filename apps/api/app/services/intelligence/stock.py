"""Stock claims (S3-F07): only from a connected, timestamped source; never inferred from the catalog file alone.
Status is ``fresh`` (snapshot younger than the staleness window), ``stale`` or ``unknown`` (no snapshot)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import utcnow
from app.models.intelligence import StockSnapshot


def latest_snapshots(db: Session, org_id: uuid.UUID, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, StockSnapshot]:
    if not product_ids:
        return {}
    rows = db.scalars(
        select(StockSnapshot)
        .where(StockSnapshot.organization_id == org_id, StockSnapshot.product_id.in_(product_ids))
        .order_by(StockSnapshot.product_id, StockSnapshot.observed_at.desc())
    ).all()
    out: dict[uuid.UUID, StockSnapshot] = {}
    for s in rows:
        out.setdefault(s.product_id, s)
    return out


def label(snapshot: StockSnapshot | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    if snapshot is None:
        return {"status": "unknown", "quantity": None, "observed_at": None, "source": None, "age_hours": None}
    age = now - snapshot.observed_at
    stale = age > timedelta(hours=get_settings().stock_stale_after_hours)
    return {
        "status": "stale" if stale else "fresh",
        "quantity": format(Decimal(snapshot.quantity).normalize(), "f"),
        "observed_at": snapshot.observed_at,
        "source": snapshot.source,
        "age_hours": round(age.total_seconds() / 3600, 1),
    }
