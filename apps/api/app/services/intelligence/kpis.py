"""KPI dashboard (S3-F04). Every metric has a written definition, an explicit date range and a currency rule:
monetary values are reported in the organisation's default currency using the tenant FX table; anything
without a rate is shown separately as an *unconverted* bucket per currency. Nothing is estimated."""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.intelligence import Order
from app.models.org import Organization
from app.models.quote import Quote, QuoteVersion
from app.models.rfq import RFQ
from app.services.intelligence.fx import load_converter
from app.services.lifecycle import TERMINAL, effective_lifecycle_status
from app.services.money import quantize
from app.services.quotes import current_version

DEFINITIONS: dict[str, str] = {
    "quote_volume": "Number of quotes created in the date range (all statuses, including drafts).",
    "conversion_rate": (
        "Quotes created in the range whose lifecycle is 'won' divided by quotes created in the range that "
        "reached a terminal lifecycle state (won, lost or withdrawn). Open quotes are excluded from both sides."
    ),
    "pending_value": (
        "Sum of the grand total of the current *approved* version of quotes that are still open (not won, lost, "
        "withdrawn or expired) as of now, regardless of creation date. Converted to the reporting currency with "
        "the FX table; amounts without a rate are listed under 'unconverted'."
    ),
    "won_value": "Sum of grand totals of quotes created in the range whose lifecycle is 'won' (same currency rule).",
    "response_time_hours": (
        "Median hours from RFQ creation to the first submission of a quote generated from that RFQ, for RFQs "
        "created in the range. RFQs without a submitted quote are excluded."
    ),
    "processing_time_hours": (
        "Median hours from quote creation to approval of its first approved version, for quotes created in the range."
    ),
    "repeat_order_rate": (
        "Customers with two or more confirmed/fulfilled orders divided by customers with at least one, counting "
        "orders confirmed in the range."
    ),
}


@dataclass
class Range:
    date_from: date
    date_to: date

    @property
    def start(self) -> datetime:
        return datetime.combine(self.date_from, time.min, tzinfo=UTC)

    @property
    def end(self) -> datetime:
        return datetime.combine(self.date_to, time.max, tzinfo=UTC)


def _median_hours(deltas: list[float]) -> float | None:
    return round(statistics.median(deltas), 2) if deltas else None


def compute(db: Session, org: Organization, rng: Range, *, today: date | None = None) -> dict[str, Any]:
    today = today or utcnow().date()
    conv = load_converter(db, org.id, org.default_currency)
    quotes = db.scalars(
        select(Quote).where(Quote.organization_id == org.id, Quote.created_at >= rng.start, Quote.created_at <= rng.end)
    ).all()
    won = lost_or_closed = 0
    won_value = Decimal("0")
    processing: list[float] = []
    for q in quotes:
        status = effective_lifecycle_status(q)
        if status in TERMINAL:
            lost_or_closed += 1
            if status == "won":
                won += 1
                v = current_version(q)
                converted = conv.convert(Decimal(v.grand_total), q.currency, q.created_at.date())
                if converted is not None:
                    won_value += converted
        approved = sorted((v for v in q.versions if v.approved_at is not None), key=lambda v: v.approved_at or utcnow())
        if approved and approved[0].approved_at is not None:
            processing.append((approved[0].approved_at - q.created_at).total_seconds() / 3600)

    # Pending value: open approved quotes as of today (not range-bound by definition)
    pending = Decimal("0")
    pending_count = 0
    open_quotes = db.scalars(select(Quote).where(Quote.organization_id == org.id, Quote.status == "approved")).all()
    for q in open_quotes:
        status = effective_lifecycle_status(q)
        if status in TERMINAL or status == "expired":
            continue
        v = current_version(q)
        if v.status != "approved":
            continue
        pending_count += 1
        converted = conv.convert(Decimal(v.grand_total), q.currency, today)
        if converted is not None:
            pending += converted

    # Response time: RFQ created → first submitted quote from it
    rfqs = db.scalars(
        select(RFQ).where(RFQ.organization_id == org.id, RFQ.created_at >= rng.start, RFQ.created_at <= rng.end)
    ).all()
    response: list[float] = []
    if rfqs:
        rfq_ids = [r.id for r in rfqs]
        firsts: dict[uuid.UUID, datetime] = {}
        for rfq_id, submitted_at in db.execute(
            select(Quote.rfq_id, QuoteVersion.submitted_at)
            .join(QuoteVersion, QuoteVersion.quote_id == Quote.id)
            .where(Quote.organization_id == org.id, Quote.rfq_id.in_(rfq_ids), QuoteVersion.submitted_at.is_not(None))
        ):
            if (
                rfq_id is not None
                and submitted_at is not None
                and (rfq_id not in firsts or submitted_at < firsts[rfq_id])
            ):
                firsts[rfq_id] = submitted_at
        for r in rfqs:
            if r.id in firsts:
                response.append((firsts[r.id] - r.created_at).total_seconds() / 3600)

    # Repeat-order rate over orders confirmed in the range
    orders = db.scalars(
        select(Order).where(
            Order.organization_id == org.id,
            Order.status.in_(("confirmed", "fulfilled")),
            Order.confirmed_at >= rng.start,
            Order.confirmed_at <= rng.end,
        )
    ).all()
    per_customer: dict[uuid.UUID, int] = {}
    for o in orders:
        if o.customer_id is not None:
            per_customer[o.customer_id] = per_customer.get(o.customer_id, 0) + 1
    repeaters = sum(1 for n in per_customer.values() if n >= 2)

    def pct(num: int, den: int) -> str | None:
        return None if den == 0 else str((Decimal(num) / Decimal(den) * 100).quantize(Decimal("0.1")))

    return {
        "date_from": rng.date_from.isoformat(),
        "date_to": rng.date_to.isoformat(),
        "reporting_currency": org.default_currency,
        "metrics": {
            "quote_volume": {"value": len(quotes)},
            "conversion_rate": {"value": pct(won, lost_or_closed), "won": won, "closed": lost_or_closed},
            "pending_value": {"value": str(pending), "count": pending_count, "currency": org.default_currency},
            "won_value": {"value": str(won_value), "currency": org.default_currency},
            "response_time_hours": {"value": _median_hours(response), "sample": len(response)},
            "processing_time_hours": {"value": _median_hours(processing), "sample": len(processing)},
            "repeat_order_rate": {
                "value": pct(repeaters, len(per_customer)),
                "repeat_customers": repeaters,
                "ordering_customers": len(per_customer),
            },
        },
        "unconverted": {cur: str(quantize(v, cur)) for cur, v in sorted(conv.unconverted.items())},
        "unconverted_count": conv.unconverted_count,
        "definitions": DEFINITIONS,
    }
