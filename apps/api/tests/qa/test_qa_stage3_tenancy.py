"""QA (independent) — S3-A09 cross-tenant leak regression for every Stage 3 identifier, list endpoint, filter
and idempotency key, plus raw-SQL RLS checks on all ten Stage 3 tables and worker isolation."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.services.intelligence import retention
from app.services.jobs import queue
from tests.conftest import Tenant, login_as
from tests.qa.test_qa_stage3_helpers import (
    approved_quote,
    connector,
    csv_bytes,
    import_csv,
    run_sql,
    seed_confirmed_order,
    sync,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]

STAGE3_TABLES = (
    "orders",
    "order_lines",
    "order_events",
    "order_proposals",
    "stock_snapshots",
    "fx_rates",
    "connector_connections",
    "sync_runs",
    "sync_conflicts",
    "deletion_requests",
)


def _populate(app, t: Tenant) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Create one of every Stage 3 object in tenant *t* and return their ids."""
    sales, approver, q = approved_quote(app, t)
    admin = login_as(app, t.users["admin"])
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    conn = connector(admin, ["read:products", "read:stock", "read:customers"])
    ts = (utcnow() - timedelta(hours=1)).isoformat()
    sync(admin, conn["id"], "stock", "stock-key", [{"sku": "SKU-1001", "quantity": "50", "observed_at": ts}])
    run = sync(admin, conn["id"], "products", "shared-key", [{"sku": "SKU-1001", "list_price": "9"}]).json()
    conflict = admin.get("/sync-conflicts").json()[0]
    imp = import_csv(
        admin, "customers", "import-key", csv_bytes(["name", "reference"], [["Imp Co", "I-1"], ["", ""]])
    ).json()
    seed_confirmed_order(t, customer_id=t.customer_id, days_ago=40)
    seed_confirmed_order(t, customer_id=t.customer_id, days_ago=20)
    proposal = sales.post(f"/customers/{t.customer_id}/order-proposals").json()
    fx = admin.post(
        "/fx-rates",
        json={"base_currency": "USD", "quote_currency": t.name and "AED", "rate": "3.6725", "as_of": "2026-01-01"},
    )
    dr = sales.post("/retention/requests", json={"customer_id": str(t.customer_id), "reason": "erasure"}).json()
    return {
        "quote": q["id"],
        "order": order["id"],
        "connector": conn["id"],
        "run": run["id"],
        "conflict": conflict["id"],
        "import_run": imp["id"],
        "proposal": proposal["id"],
        "fx_status": str(fx.status_code),
        "deletion": dr["id"],
        "customer": str(t.customer_id),
    }


def test_every_stage3_id_from_another_tenant_is_404(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    ids = _populate(app, tenant_a)
    owner_b = login_as(app, tenant_b.users["owner"])
    attempts = [
        ("GET", f"/orders/{ids['order']}", None),
        ("POST", f"/orders/{ids['order']}/confirm", {}),
        ("POST", f"/orders/{ids['order']}/fulfil", {}),
        ("POST", f"/orders/{ids['order']}/cancel", {"reason": "pwn"}),
        ("POST", f"/quotes/{ids['quote']}/orders", {}),
        ("POST", f"/order-proposals/{ids['proposal']}/approve", {}),
        ("POST", f"/order-proposals/{ids['proposal']}/reject", {"reason": "pwn"}),
        ("POST", f"/connectors/{ids['connector']}/sync", {"entity": "stock", "idempotency_key": "z", "records": []}),
        ("POST", f"/connectors/{ids['connector']}/disable", None),
        ("POST", f"/sync-conflicts/{ids['conflict']}/resolve", {"resolution": "apply_remote"}),
        ("GET", f"/data/runs/{ids['run']}/failed.csv", None),
        ("GET", f"/data/runs/{ids['import_run']}/failed.csv", None),
        ("POST", f"/retention/requests/{ids['deletion']}/approve", {}),
        ("POST", f"/retention/requests/{ids['deletion']}/reject", {"reason": "pwn"}),
        ("POST", f"/retention/requests/{ids['deletion']}/legal-hold", {"hold": True}),
        ("POST", "/retention/requests", {"customer_id": ids["customer"], "reason": "pwn"}),
        ("GET", f"/customers/{ids['customer']}/profile", None),
        ("POST", f"/customers/{ids['customer']}/order-proposals", None),
    ]
    for method, path, body in attempts:
        r = (
            owner_b.get(path)
            if method == "GET"
            else owner_b.post(path, json=body)
            if body is not None
            else owner_b.post(path)
        )
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    # nothing changed on tenant A's side
    owner_a = login_as(app, tenant_a.users["owner"])
    assert owner_a.get(f"/orders/{ids['order']}").json()["status"] == "proposed"
    assert owner_a.get("/sync-conflicts").json()[0]["status"] == "open"
    assert next(r for r in owner_a.get("/retention/requests").json())["status"] == "requested"
    assert next(c for c in owner_a.get("/connectors").json())["status"] == "active"


def test_lists_filters_and_keys_never_cross_tenants(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    ids = _populate(app, tenant_a)
    owner_b = login_as(app, tenant_b.users["owner"])
    assert owner_b.get("/orders").json() == [] and owner_b.get(f"/orders?customer_id={ids['customer']}").json() == []
    assert owner_b.get("/order-proposals").json() == [] and owner_b.get("/order-proposals?status=proposed").json() == []
    assert owner_b.get("/sync-conflicts").json() == [] and owner_b.get("/sync-conflicts?status=").json() == []
    assert owner_b.get("/connectors").json() == [] and owner_b.get("/data/runs").json() == []
    assert owner_b.get("/fx-rates").json() == [] and owner_b.get("/retention/requests").json() == []
    assert {s["status"] for s in owner_b.get("/stock").json()} == {"unknown"}  # A's snapshots invisible
    k = owner_b.get("/kpis").json()
    assert k["metrics"]["quote_volume"]["value"] == 0 and k["unconverted"] == {} and k["reporting_currency"] == "EUR"
    assert owner_b.get(f"/customers/{ids['customer']}/profile").status_code == 404
    # idempotency keys are per tenant: B re-using A's keys gets its own runs, never A's outcome
    conn_b = connector(owner_b, ["read:products"])
    run_b = sync(owner_b, conn_b["id"], "products", "shared-key", [{"sku": "SKU-1001", "list_price": "9"}]).json()
    assert run_b["id"] != ids["run"] and run_b["conflicts"] == 1
    imp_b = import_csv(owner_b, "customers", "import-key", csv_bytes(["name"], [["B Co"]])).json()
    assert imp_b["id"] != ids["import_run"] and imp_b["applied"] == 1
    assert owner_b.get("/sync-conflicts").json()[0]["sync_run_id"] == run_b["id"]
    assert {c["name"] for c in owner_b.get("/customers").json()} >= {"B Co"} and "Imp Co" not in {
        c["name"] for c in owner_b.get("/customers").json()
    }
    # A still sees exactly its own objects
    admin_a = login_as(app, tenant_a.users["admin"])
    assert [c["sync_run_id"] for c in admin_a.get("/sync-conflicts").json()] == [ids["run"]]
    assert len(admin_a.get("/data/runs").json()) == 3


def test_rls_hides_every_stage3_table_and_refuses_foreign_inserts(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    _populate(app, tenant_a)
    counts_a: dict[str, int] = {}
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        for table in STAGE3_TABLES:
            counts_a[table] = int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)  # noqa: S608
        db.rollback()
    assert all(v > 0 for v in counts_a.values()), counts_a  # fixture really populated every table
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_b.org_id, None)
        for table in STAGE3_TABLES:
            assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table  # noqa: S608
        # B cannot update, delete or insert A's rows even with raw SQL (0 rows affected / WITH CHECK violation)
        assert db.execute(text("UPDATE orders SET status = 'confirmed'")).rowcount == 0
        assert db.execute(text("DELETE FROM sync_conflicts")).rowcount == 0
        assert db.execute(text("UPDATE deletion_requests SET legal_hold = false")).rowcount == 0
        with pytest.raises(Exception):  # noqa: B017
            db.execute(
                text(
                    "INSERT INTO fx_rates (id, organization_id, base_currency, quote_currency, rate, as_of, source, created_at) "
                    "VALUES (:id, :org, 'USD', 'AED', 1, '2026-01-01', 'manual', now())"
                ),
                {"id": str(uuid.uuid4()), "org": str(tenant_a.org_id)},
            )
        db.rollback()
    # worker purge in A never touches B's data
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        for table in STAGE3_TABLES:
            assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar() == counts_a[table], table  # noqa: S608
        db.rollback()


def test_worker_purges_only_the_requesting_tenant(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    for t in (tenant_a, tenant_b):
        sales = login_as(app, t.users["sales"])
        sales.post(
            f"/customers/{t.customer_id}/contacts",
            json={"full_name": "Contact", "email": f"c@{t.name.lower().replace(' ', '')}.example"},
        )
    sales_a = login_as(app, tenant_a.users["sales"])
    admin_a = login_as(app, tenant_a.users["admin"])
    req = sales_a.post("/retention/requests", json={"customer_id": str(tenant_a.customer_id), "reason": "erase"}).json()
    admin_a.post(f"/retention/requests/{req['id']}/approve", json={})
    run_sql(
        tenant_a.org_id,
        "UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id",
        {"id": req["id"]},
    )
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1
        assert queue.run_once(db, "qa-worker") == 1
    assert (
        login_as(app, tenant_a.users["owner"])
        .get(f"/customers/{tenant_a.customer_id}")
        .json()["name"]
        .startswith("Deleted customer")
    )
    b = login_as(app, tenant_b.users["owner"])
    assert b.get(f"/customers/{tenant_b.customer_id}").json()["name"] == "Beta Supplies Customer"
    assert len(b.get(f"/customers/{tenant_b.customer_id}/contacts").json()) == 1
    assert b.get("/retention/requests").json() == []
