"""Shared helpers for the independent QA Stage 3 suite (this module defines no tests).

QA derives expectations from docs/PRD.md §Stage 3 (S3-F01..F08, S3-A01..A09) and docs/API_CONTRACT.md; the product
code is only consulted to locate entry points. Synthetic data only.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.models.audit import AuditEvent
from app.models.intelligence import Order, OrderEvent, OrderLine
from tests.conftest import AuthedClient, Tenant, login_as
from tests.integration.test_quotes import make_quote

# 500 x 0.85 = 425.00 net, 5 % tax = 21.25, grand total 446.25 AED (demo catalog, tenant default tax 5 %)
ONE_LINE_RFQ = "1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated\n"
ONE_LINE_TOTAL = Decimal("446.25")


def approved_quote(app, tenant: Tenant, rfq_text: str = ONE_LINE_RFQ, *, sales=None, approver=None):  # type: ignore[no-untyped-def]
    """sales creates + submits, approver approves; returns (sales, approver, approved quote json)."""
    sales = sales or login_as(app, tenant.users["sales"])
    approver = approver or login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id), rfq_text)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert a.status_code == 200, a.text
    return sales, approver, a.json()


def transition(ac: AuthedClient, q: dict[str, Any], to: str, evidence: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"expected_lock_version": q["lock_version"], "to_status": to}
    if evidence:
        body["evidence"] = evidence
    r = ac.post(f"/quotes/{q['id']}/transition", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def audit_rows(org_id: uuid.UUID, prefix: str) -> list[dict[str, Any]]:
    """Audit events of one tenant whose action starts with *prefix*, oldest first."""
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = db.execute(
            select(AuditEvent.action, AuditEvent.actor_user_id, AuditEvent.entity_id, AuditEvent.summary)
            .where(AuditEvent.organization_id == org_id, AuditEvent.action.like(f"{prefix}%"))
            .order_by(AuditEvent.id)
        ).all()
        db.rollback()
    return [
        {"action": a, "actor": str(u) if u else None, "entity_id": str(e) if e else None, "summary": s}
        for a, u, e, s in rows
    ]


def run_sql(org_id: uuid.UUID, statement: str, params: dict[str, Any] | None = None) -> Any:
    """Execute one statement inside the tenant context (test fixture plumbing, not a product path)."""
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        result = db.execute(text(statement), params or {})
        rows = result.all() if result.returns_rows else None
        db.commit()
    return rows


def seed_confirmed_order(
    tenant: Tenant,
    *,
    customer_id: uuid.UUID,
    days_ago: int,
    sku: str = "SKU-1001",
    qty: str = "500",
    unit_price: str = "0.85",
    cost_price: str | None = "0.52",
    currency: str = "AED",
    number: str | None = None,
) -> uuid.UUID:
    """Synthetic historical *confirmed* order (what an ERP import would have produced)."""
    with get_sessionmaker()() as db:
        set_tenant(db, tenant.org_id, None)
        when = utcnow() - timedelta(days=days_ago)
        net = Decimal(qty) * Decimal(unit_price)
        tax = (net * Decimal("0.05")).quantize(Decimal("0.01"))
        o = Order(
            organization_id=tenant.org_id,
            number=number or f"SO-QA-{uuid.uuid4().hex[:8]}",
            customer_id=customer_id,
            currency=currency,
            status="confirmed",
            source="import",
            confirmed_at=when,
            created_at=when,
            subtotal=net,
            tax_total=tax,
            grand_total=net + tax,
        )
        o.lines.append(
            OrderLine(
                organization_id=tenant.org_id,
                line_no=1,
                sku=sku,
                description=f"{sku} synthetic history",
                quantity=Decimal(qty),
                unit="EA",
                unit_price=Decimal(unit_price),
                cost_price=Decimal(cost_price) if cost_price else None,
                tax_rate_pct=Decimal("5"),
                line_total=net + tax,
            )
        )
        o.events.append(OrderEvent(organization_id=tenant.org_id, kind="confirmed", source="import", occurred_at=when))
        db.add(o)
        db.commit()
        return o.id


def parse_csv(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def import_csv(ac: AuthedClient, entity: str, key: str, content: bytes, filename: str = "data.csv"):  # type: ignore[no-untyped-def]
    return ac.post(
        f"/data/import/{entity}?idempotency_key={key}", files={"file": (filename, content, "text/csv")}, data={}
    )


def connector(admin: AuthedClient, scopes: list[str], kind: str = "erp_stub", name: str = "QA ERP") -> dict[str, Any]:
    r = admin.post("/connectors", json={"kind": kind, "name": name, "scopes": scopes})
    assert r.status_code == 201, r.text
    return r.json()


def sync(admin: AuthedClient, conn_id: str, entity: str, key: str, records: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    return admin.post(
        f"/connectors/{conn_id}/sync", json={"entity": entity, "idempotency_key": key, "records": records}
    )
