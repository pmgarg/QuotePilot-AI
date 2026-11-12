"""Stage 3 acceptance tests S3-A01..A09 (see docs/PRD.md) plus role, audit and RLS checks."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant
from app.models.audit import AuditEvent
from app.models.catalog import Product
from app.models.customer import Customer
from app.models.followup import Contact
from app.models.intelligence import DeletionRequest, Order, OrderEvent, OrderLine
from app.services.intelligence import orders as order_service
from app.services.intelligence import retention
from app.services.jobs import queue
from tests.conftest import AuthedClient, Tenant, login_as, upload
from tests.integration.test_quotes import make_quote

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ helpers
def approved_quote(app, tenant: Tenant):  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    return sales, approver, a.json()


def transition(ac: AuthedClient, q: dict, to: str, evidence: str | None = None) -> dict:  # type: ignore[type-arg]
    body: dict = {"expected_lock_version": q["lock_version"], "to_status": to}  # type: ignore[type-arg]
    if evidence:
        body["evidence"] = evidence
    r = ac.post(f"/quotes/{q['id']}/transition", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def audit_actions(org_id: uuid.UUID, prefix: str) -> list[str]:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = db.scalars(
            select(AuditEvent.action).where(AuditEvent.organization_id == org_id, AuditEvent.action.like(f"{prefix}%"))
        ).all()
        db.rollback()
    return list(rows)


def seed_confirmed_orders(tenant: Tenant, days_ago: list[int], sku: str = "SKU-1001", qty: str = "500") -> None:
    """Confirmed historical orders for the repeat-order assistant (synthetic history)."""
    with get_sessionmaker()() as db:
        set_tenant(db, tenant.org_id, None)
        for d in days_ago:
            when = utcnow() - timedelta(days=d)
            o = Order(
                organization_id=tenant.org_id,
                number=f"SO-HIST-{d}",
                customer_id=tenant.customer_id,
                currency="AED",
                status="confirmed",
                source="import",
                confirmed_at=when,
                created_at=when,
                subtotal=Decimal("425"),
                tax_total=Decimal("21.25"),
                grand_total=Decimal("446.25"),
            )
            o.lines.append(
                OrderLine(
                    organization_id=tenant.org_id,
                    line_no=1,
                    sku=sku,
                    description="Hex bolt M8x40 zinc plated",
                    quantity=Decimal(qty),
                    unit="EA",
                    unit_price=Decimal("0.85"),
                    cost_price=Decimal("0.52"),
                    tax_rate_pct=Decimal("5"),
                    line_total=Decimal("446.25"),
                )
            )
            o.events.append(
                OrderEvent(organization_id=tenant.org_id, kind="confirmed", source="import", occurred_at=when)
            )
            db.add(o)
        db.commit()


# ------------------------------------------------------- S3-F02 orders & gate
def test_quote_to_order_requires_separate_confirmation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    readonly = login_as(app, tenant_a.users["readonly"])
    assert readonly.post(f"/quotes/{q['id']}/orders", json={}).status_code == 403
    r = sales.post(f"/quotes/{q['id']}/orders", json={"notes": "customer PO 4711"})
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["status"] == "proposed" and order["source"] == "quote" and order["number"].startswith("SO-")
    assert [e["kind"] for e in order["events"]] == ["proposed"]
    # prices are copied from the approved document, never recomputed
    q_lines = {ln["sku"]: ln for ln in q["current_version"]["lines"]}
    for ln in order["lines"]:
        assert Decimal(ln["unit_price"]) == Decimal(q_lines[ln["sku"]]["unit_price"])
        assert "cost_price" not in ln  # S3-A02 for sales
    assert order["grand_total"] == q["current_version"]["grand_total"]
    # a second open order for the same quote is refused
    assert sales.post(f"/quotes/{q['id']}/orders", json={}).status_code == 409
    # sales cannot confirm; approver can; proposer cannot confirm own proposal
    assert sales.post(f"/orders/{order['id']}/confirm", json={}).status_code == 403
    approver_view = approver.get(f"/orders/{order['id']}").json()
    assert all("cost_price" in ln for ln in approver_view["lines"])
    c = approver.post(f"/orders/{order['id']}/confirm", json={})
    assert c.status_code == 200 and c.json()["status"] == "confirmed" and c.json()["confirmed_at"]
    assert approver.post(f"/orders/{order['id']}/confirm", json={}).status_code == 409
    assert approver.post(f"/orders/{order['id']}/cancel", json={}).status_code == 422  # reason required
    f = sales.post(f"/orders/{order['id']}/fulfil", json={"reason": "shipped 3 cartons"})
    assert f.status_code == 200 and [e["kind"] for e in f.json()["events"]] == ["proposed", "confirmed", "fulfilled"]
    assert sales.get("/orders?status=fulfilled").json()[0]["id"] == order["id"]
    assert {"order.propose", "order.confirmed", "order.fulfilled"} <= set(audit_actions(tenant_a.org_id, "order."))


def test_self_proposed_order_cannot_be_self_confirmed(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, approver, q = approved_quote(app, tenant_a)
    order = approver.post(f"/quotes/{q['id']}/orders", json={}).json()
    assert approver.post(f"/orders/{order['id']}/confirm", json={}).status_code == 403
    owner = login_as(app, tenant_a.users["owner"])
    assert owner.post(f"/orders/{order['id']}/confirm", json={}).status_code == 200


def test_unapproved_quote_cannot_become_order(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    assert sales.post(f"/quotes/{q['id']}/orders", json={}).status_code == 409


# --------------------------------------------------- S3-F03 / S3-A08 repeat orders
def test_repeat_order_proposal_has_evidence_and_full_audit_trail(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    cid = str(tenant_a.customer_id)
    # no history → nothing proposed (never invented)
    r = sales.post(f"/customers/{cid}/order-proposals")
    assert r.status_code == 201 and r.json() is None
    seed_confirmed_orders(tenant_a, days_ago=[90, 60, 30])
    p = sales.post(f"/customers/{cid}/order-proposals").json()
    assert p["status"] == "proposed" and p["kind"] == "repeat"
    assert p["lines"][0]["sku"] == "SKU-1001" and p["lines"][0]["times_ordered"] == 3
    assert any("ordered 3×" in e for e in p["evidence"]) and any(
        "own" in e.lower() or "this organisation" in e for e in p["evidence"]
    )
    assert sales.post(f"/customers/{cid}/order-proposals").status_code == 409  # one open proposal at a time
    assert sales.post(f"/order-proposals/{p['id']}/approve", json={}).status_code == 403
    assert approver.post(f"/order-proposals/{p['id']}/reject", json={}).status_code == 422
    a = approver.post(f"/order-proposals/{p['id']}/approve", json={"reason": "customer confirmed by phone"})
    assert a.status_code == 200 and a.json()["status"] == "converted" and a.json()["order_id"]
    order = approver.get(f"/orders/{a.json()['order_id']}").json()
    assert order["status"] == "proposed" and order["source"] == "repeat"  # still needs the confirmation gate
    assert order["lines"][0]["unit_price"] == "0.85" and order["lines"][0]["product_id"]  # priced from active catalog
    assert approver.post(f"/order-proposals/{p['id']}/approve", json={}).status_code == 409
    actions = audit_actions(tenant_a.org_id, "order")
    assert {"order_proposal.generate", "order_proposal.approve", "order.propose"} <= set(actions)
    profile = sales.get(f"/customers/{cid}/profile").json()
    assert profile["stats"]["confirmed_orders"] == 3 and "avg_margin_pct" not in profile["stats"]
    assert "avg_margin_pct" in approver.get(f"/customers/{cid}/profile").json()["stats"]


# ---------------------------------------------- S3-F04 / S3-A01 / S3-A03 KPIs
def test_kpis_match_fixture_and_apply_currency_rule(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, won_q = approved_quote(app, tenant_a)
    won_q = transition(sales, won_q, "sent")
    transition(sales, won_q, "won", "signed PO received")
    _, _, lost_q = approved_quote(app, tenant_a)
    transition(sales, lost_q, "lost", "went with competitor")
    _, _, pending_q = approved_quote(app, tenant_a)
    _, _, usd_q = approved_quote(app, tenant_a)
    with get_sessionmaker()() as db:  # synthetic: a pending quote in another currency
        set_tenant(db, tenant_a.org_id, None)
        db.execute(text("UPDATE quotes SET currency = 'USD' WHERE id = :id"), {"id": usd_q["id"]})
        db.commit()
    total = Decimal(pending_q["current_version"]["grand_total"])
    k = sales.get("/kpis").json()
    m = k["metrics"]
    assert m["quote_volume"]["value"] == 4
    assert m["conversion_rate"] == {"value": "50.0", "won": 1, "closed": 2}
    assert m["pending_value"]["count"] == 2 and Decimal(m["pending_value"]["value"]) == total
    assert k["unconverted"] == {"USD": str(total)} and k["unconverted_count"] == 1
    assert m["processing_time_hours"]["sample"] == 4 and m["response_time_hours"]["sample"] == 4
    assert set(k["definitions"]) >= set(m)
    # FX table is owner/admin only; after a rate exists the USD quote converts, nothing stays unconverted
    assert (
        sales.post(
            "/fx-rates", json={"base_currency": "USD", "quote_currency": "AED", "rate": "3.6725", "as_of": "2026-01-01"}
        ).status_code
        == 403
    )
    admin = login_as(app, tenant_a.users["admin"])
    r = admin.post(
        "/fx-rates", json={"base_currency": "USD", "quote_currency": "AED", "rate": "3.6725", "as_of": "2026-01-01"}
    )
    assert r.status_code == 201, r.text
    assert (
        admin.post(
            "/fx-rates", json={"base_currency": "USD", "quote_currency": "AED", "rate": "3.6725", "as_of": "2026-01-01"}
        ).status_code
        == 409
    )
    k2 = sales.get("/kpis").json()
    expected = total + (total * Decimal("3.6725")).quantize(Decimal("0.01"))
    assert Decimal(k2["metrics"]["pending_value"]["value"]) == expected and k2["unconverted"] == {}
    assert sales.get("/kpis?date_from=2026-02-01&date_to=2026-01-01").status_code == 422
    assert sales.get("/kpis?date_from=2020-01-01&date_to=2020-01-31").json()["metrics"]["quote_volume"]["value"] == 0


# ---------------------------------------- S3-F06 / S3-A04 connectors & conflicts
def _connector(admin: AuthedClient, scopes: list[str]) -> dict:  # type: ignore[type-arg]
    r = admin.post("/connectors", json={"kind": "erp_stub", "name": "Demo ERP", "scopes": scopes})
    assert r.status_code == 201, r.text
    return r.json()


def test_sync_conflicts_never_overwrite_master_data(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    assert (
        sales.post("/connectors", json={"kind": "erp_stub", "name": "x", "scopes": ["read:stock"]}).status_code == 403
    )
    assert (
        admin.post("/connectors", json={"kind": "erp_stub", "name": "x", "scopes": ["write:everything"]}).status_code
        == 422
    )
    conn = _connector(admin, ["read:products", "read:stock"])
    assert (
        admin.post("/connectors", json={"kind": "erp_stub", "name": "dup", "scopes": ["read:stock"]}).status_code == 409
    )
    # scope enforcement
    assert (
        admin.post(
            f"/connectors/{conn['id']}/sync", json={"entity": "orders", "idempotency_key": "o1", "records": []}
        ).status_code
        == 403
    )
    records = [
        {"sku": "SKU-1001", "list_price": "0.99", "description": "Hex bolt M8x40 zinc plated"},  # price differs
        {"sku": "SKU-1002", "list_price": "1.10"},  # identical
        {"sku": "NEW-9999", "description": "Unknown part", "list_price": "5"},  # new SKU
        {"list_price": "1"},  # invalid
    ]
    run = admin.post(
        f"/connectors/{conn['id']}/sync", json={"entity": "products", "idempotency_key": "p1", "records": records}
    ).json()
    assert (run["received"], run["applied"], run["conflicts"], run["rejected"], run["status"]) == (
        4,
        1,
        2,
        1,
        "partial",
    )
    replay = admin.post(
        f"/connectors/{conn['id']}/sync", json={"entity": "products", "idempotency_key": "p1", "records": records}
    ).json()
    assert replay["id"] == run["id"]  # idempotent
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        price = db.scalar(
            select(Product.list_price).where(Product.sku == "SKU-1001", Product.organization_id == tenant_a.org_id)
        )
        db.rollback()
    assert price == Decimal("0.850000")  # untouched
    conflicts = admin.get("/sync-conflicts").json()
    assert {c["entity_key"]: c["fields"] for c in conflicts} == {"SKU-1001": ["list_price"], "NEW-9999": ["new"]}
    assert sales.get("/sync-conflicts").status_code == 403
    bolt = next(c for c in conflicts if c["entity_key"] == "SKU-1001")
    new = next(c for c in conflicts if c["entity_key"] == "NEW-9999")
    assert admin.post(f"/sync-conflicts/{new['id']}/resolve", json={"resolution": "apply_remote"}).status_code == 409
    r = admin.post(
        f"/sync-conflicts/{bolt['id']}/resolve", json={"resolution": "apply_remote", "note": "ERP is master"}
    )
    assert r.status_code == 200 and r.json()["status"] == "resolved_remote"
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        price = db.scalar(
            select(Product.list_price).where(Product.sku == "SKU-1001", Product.organization_id == tenant_a.org_id)
        )
        db.rollback()
    assert price == Decimal("0.990000")
    assert admin.post(f"/sync-conflicts/{bolt['id']}/resolve", json={"resolution": "keep_local"}).status_code == 409
    assert admin.get("/sync-conflicts?status=open").json()[0]["id"] == new["id"]
    assert "sync_conflict.resolve" in audit_actions(tenant_a.org_id, "sync_conflict")


def test_connector_orders_arrive_as_proposed(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin, ["read:orders"])
    recs = [
        {
            "external_ref": "ERP-1",
            "currency": "AED",
            "lines": [{"sku": "SKU-1001", "quantity": "10", "unit_price": "0.85"}],
        },
        {"external_ref": "ERP-2", "lines": [{"sku": "SKU-1001", "quantity": "-1", "unit_price": "0.85"}]},
        {
            "external_ref": "ERP-3",
            "customer_reference": "NOPE",
            "lines": [{"sku": "X", "quantity": 1, "unit_price": 1}],
        },
    ]
    run = admin.post(
        f"/connectors/{conn['id']}/sync", json={"entity": "orders", "idempotency_key": "o1", "records": recs}
    ).json()
    assert (run["applied"], run["rejected"]) == (1, 2)
    orders = admin.get("/orders?status=proposed").json()
    assert len(orders) == 1 and orders[0]["source"] == "connector"
    again = admin.post(
        f"/connectors/{conn['id']}/sync", json={"entity": "orders", "idempotency_key": "o2", "records": recs[:1]}
    ).json()
    assert again["applied"] == 1 and len(admin.get("/orders").json()) == 1  # external_ref idempotent


# ------------------------------------------------------- S3-F07 / S3-A05 stock
def test_stock_is_only_claimed_from_timestamped_sources(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    before = {s["sku"]: s["status"] for s in sales.get("/stock").json()}
    assert set(before.values()) == {"unknown"}  # catalog stock column is not a live claim
    conn = _connector(admin, ["read:stock"])
    now = utcnow()
    recs = [
        {"sku": "SKU-1001", "quantity": "120", "observed_at": (now - timedelta(hours=1)).isoformat()},
        {"sku": "SKU-1002", "quantity": "5", "observed_at": (now - timedelta(hours=48)).isoformat()},
        {"sku": "SKU-1003", "quantity": "5"},  # no timestamp
        {"sku": "SKU-1004", "quantity": "5", "observed_at": "2026-01-01T00:00:00"},  # naive timestamp
        {"sku": "SKU-1005", "quantity": "5", "observed_at": (now + timedelta(days=1)).isoformat()},  # future
    ]
    run = admin.post(
        f"/connectors/{conn['id']}/sync", json={"entity": "stock", "idempotency_key": "s1", "records": recs}
    ).json()
    assert (run["applied"], run["rejected"]) == (2, 3)
    levels = {s["sku"]: s for s in sales.get("/stock").json()}
    assert levels["SKU-1001"]["status"] == "fresh" and levels["SKU-1001"]["quantity"] == "120"
    assert levels["SKU-1002"]["status"] == "stale" and levels["SKU-1002"]["age_hours"] >= 48
    assert levels["SKU-1003"]["status"] == "unknown" and levels["SKU-1003"]["quantity"] is None
    assert levels["SKU-1001"]["source"] == "erp_stub"


# ---------------------------------------------- S3-F05 / S3-A06 / S3-A07 data I/O
def test_bulk_export_is_role_gated_audited_and_formula_safe(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    readonly = login_as(app, tenant_a.users["readonly"])
    approver = login_as(app, tenant_a.users["approver"])
    owner = login_as(app, tenant_a.users["owner"])
    for ac in (sales, readonly):
        for entity in ("customers", "orders", "products"):
            assert ac.get(f"/data/export/{entity}").status_code == 403
    assert audit_actions(tenant_a.org_id, "export.denied").count("export.denied") == 6
    # a formula-looking description lands in an order via import, export neutralises it
    admin = login_as(app, tenant_a.users["admin"])
    csv_in = (
        b"order_ref,customer_reference,sku,description,quantity,unit,unit_price,currency\r\n"
        b'PO-1,,SKU-1001,"=HYPERLINK(""http://evil"")",10,EA,0.85,AED\r\n'
    )
    r = upload(admin, "/data/import/orders?idempotency_key=imp-1", "orders.csv", csv_in, content_type="text/csv")
    assert r.status_code == 201, r.text
    exp = approver.get("/data/export/orders")
    assert exp.status_code == 200 and exp.headers["content-type"].startswith("text/csv")
    body = exp.content.decode("utf-8-sig")
    assert "\"'=HYPERLINK" in body and "cost_price" in body.splitlines()[0]  # approver may see costs
    prod = owner.get("/data/export/products").content.decode("utf-8-sig")
    assert "cost_price" in prod.splitlines()[0] and "SKU-1001" in prod
    assert owner.get("/data/export/customers").status_code == 200
    assert owner.get("/data/export/secrets").status_code == 404
    assert {"export.orders", "export.products", "export.customers"} <= set(audit_actions(tenant_a.org_id, "export."))


def test_partial_import_recovery(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    csv_in = (
        b"name,reference,email,default_currency\r\n"
        b"Good Co,GC-1,buyer@good.example,AED\r\n"
        b"Bad Currency,BC-1,,ZZZ\r\n"
        b"No Email Co,NE-1,not-an-email,USD\r\n"
        b"Fine Ltd,FL-1,,\r\n"
    )
    assert upload(sales, "/data/import/customers?idempotency_key=c1", "c.csv", csv_in).status_code == 403
    r = upload(admin, "/data/import/customers?idempotency_key=c1", "c.csv", csv_in)
    assert r.status_code == 201, r.text
    run = r.json()
    assert (run["received"], run["applied"], run["rejected"], run["status"]) == (4, 2, 2, "partial")
    assert {f["row"]: f["errors"] for f in run["report"]["failed_rows"]} == {
        3: ["unsupported currency ZZZ"],
        4: ["invalid email"],
    }
    names = {c["name"] for c in admin.get("/customers").json()}
    assert {"Good Co", "Fine Ltd"} <= names and "Bad Currency" not in names
    # same key → same run, nothing duplicated
    assert upload(admin, "/data/import/customers?idempotency_key=c1", "c.csv", csv_in).json()["id"] == run["id"]
    failed = admin.get(f"/data/runs/{run['id']}/failed.csv")
    assert failed.status_code == 200
    lines = failed.content.decode("utf-8-sig").splitlines()
    assert lines[0].endswith('"_row","_errors"') and len(lines) == 3 and "Bad Currency" in lines[1]
    # fix and re-upload only the failed rows
    fixed = (
        b"name,reference,email,default_currency\r\nBad Currency,BC-1,,AED\r\nNo Email Co,NE-1,buyer@ne.example,USD\r\n"
    )
    r2 = upload(admin, "/data/import/customers?idempotency_key=c2", "c.csv", fixed).json()
    assert (r2["applied"], r2["rejected"], r2["status"]) == (2, 0, "completed")
    # duplicate reference now rejected
    r3 = upload(admin, "/data/import/customers?idempotency_key=c3", "c.csv", fixed).json()
    assert r3["applied"] == 0 and r3["status"] == "failed"
    assert upload(admin, "/data/import/customers?idempotency_key=c4", "c.csv", b"foo,bar\r\n1,2\r\n").status_code == 422
    assert upload(admin, "/data/import/products?idempotency_key=c5", "p.csv", csv_in).status_code == 404
    # orders import groups rows per order_ref and creates *proposed* orders
    orders_csv = (
        b"order_ref,customer_reference,sku,description,quantity,unit,unit_price,currency\r\n"
        b"PO-A,GC-1,SKU-1001,Bolt,100,EA,0.85,AED\r\n"
        b"PO-A,GC-1,SKU-1003,Nut,100,EA,0.25,AED\r\n"
        b"PO-B,MISSING,SKU-1001,Bolt,1,EA,0.85,AED\r\n"
        b"PO-C,,SKU-1001,Bolt,abc,EA,0.85,AED\r\n"
    )
    ro = upload(admin, "/data/import/orders?idempotency_key=o1", "o.csv", orders_csv).json()
    assert (ro["applied"], ro["rejected"]) == (2, 2)
    orders = admin.get("/orders").json()
    assert len(orders) == 1 and orders[0]["status"] == "proposed" and orders[0]["source"] == "import"
    assert len(admin.get("/data/runs").json()) == 4  # c4/c5 were rejected before a run was created
    assert len(audit_actions(tenant_a.org_id, "import.")) == 4


# --------------------------------------------------------- S3-F08 retention
def test_retention_workflow_soft_delete_then_worker_purge(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    owner = login_as(app, tenant_a.users["owner"])
    cid = str(tenant_a.customer_id)
    sales.post(f"/customers/{cid}/contacts", json={"full_name": "Buyer", "email": "buyer@customer.example"})
    _, _, q = approved_quote(app, tenant_a)
    r = sales.post("/retention/requests", json={"customer_id": cid, "reason": "customer asked for erasure"})
    assert r.status_code == 201, r.text
    req = r.json()
    assert sales.get("/retention/requests").status_code == 403
    assert sales.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 403
    assert sales.post("/retention/requests", json={"customer_id": cid, "reason": "again"}).status_code == 409
    # the requester cannot approve their own request (admin requests a 2nd customer to prove it)
    c2 = admin.post("/customers", json={"name": "Second Co"}).json()
    r2 = admin.post("/retention/requests", json={"customer_id": c2["id"], "reason": "dup"}).json()
    assert admin.post(f"/retention/requests/{r2['id']}/approve", json={}).status_code == 403
    assert owner.post(f"/retention/requests/{r2['id']}/reject", json={}).status_code == 422
    assert (
        owner.post(f"/retention/requests/{r2['id']}/reject", json={"reason": "still a live account"}).json()["status"]
        == "rejected"
    )
    # legal hold blocks approval
    assert admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": True}).json()["legal_hold"] is True
    assert admin.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 409
    admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": False})
    a = admin.post(f"/retention/requests/{req['id']}/approve", json={})
    assert a.status_code == 200 and a.json()["status"] == "approved" and a.json()["purge_after"]
    assert a.json()["summary"]["soft_deleted"]["contacts_opted_out"] == 1
    assert cid not in {c["id"] for c in sales.get("/customers").json()}  # hidden immediately
    assert sales.get(f"/customers/{cid}").status_code == 404  # SEC3-02: no further processing by ordinary roles
    assert admin.get(f"/customers/{cid}").json()["deleted_at"]  # retention managers still see it
    # grace period not elapsed → worker does nothing
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 0
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            text("UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id"),
            {"id": req["id"]},
        )
        db.commit()
        assert retention.enqueue_due_purges(db) == 1
        assert retention.enqueue_due_purges(db) == 0  # idempotent
        assert queue.run_once(db, "test-worker") == 1
        set_tenant(db, tenant_a.org_id, None)
        cust = db.get(Customer, tenant_a.customer_id)
        assert cust is not None and cust.name.startswith("Deleted customer") and cust.email is None
        assert db.scalar(select(Contact).where(Contact.customer_id == tenant_a.customer_id)) is None
        dr = db.get(DeletionRequest, uuid.UUID(req["id"]))
        assert dr is not None and dr.status == "executed" and dr.summary["contacts_deleted"] == 1
        db.rollback()
    assert admin.get(f"/quotes/{q['id']}").status_code == 200  # commercial record kept
    assert {
        "retention.request",
        "retention.approve",
        "retention.reject",
        "retention.legal_hold",
        "retention.purged",
    } <= set(audit_actions(tenant_a.org_id, "retention."))


# ------------------------------------------------- S3-A09 cross-tenant regression
def test_stage3_cross_tenant_ids_are_404_and_rls_holds(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, approver_a, q = approved_quote(app, tenant_a)
    order = approver_a.post(f"/quotes/{q['id']}/orders", json={}).json()
    admin_a = login_as(app, tenant_a.users["admin"])
    conn = _connector(admin_a, ["read:products"])
    run = admin_a.post(
        f"/connectors/{conn['id']}/sync",
        json={"entity": "products", "idempotency_key": "x", "records": [{"sku": "SKU-1001", "list_price": "9"}]},
    ).json()
    conflict = admin_a.get("/sync-conflicts").json()[0]
    seed_confirmed_orders(tenant_a, [40, 20])
    sales_a = login_as(app, tenant_a.users["sales"])
    proposal = sales_a.post(f"/customers/{tenant_a.customer_id}/order-proposals").json()
    dr = sales_a.post(
        "/retention/requests", json={"customer_id": str(tenant_a.customer_id), "reason": "erasure"}
    ).json()
    owner_b = login_as(app, tenant_b.users["owner"])
    checks = [
        owner_b.get(f"/orders/{order['id']}"),
        owner_b.post(f"/orders/{order['id']}/confirm", json={}),
        owner_b.post(f"/quotes/{q['id']}/orders", json={}),
        owner_b.post(f"/order-proposals/{proposal['id']}/approve", json={}),
        owner_b.post(
            f"/connectors/{conn['id']}/sync", json={"entity": "products", "idempotency_key": "y", "records": []}
        ),
        owner_b.post(f"/sync-conflicts/{conflict['id']}/resolve", json={"resolution": "keep_local"}),
        owner_b.get(f"/data/runs/{run['id']}/failed.csv"),
        owner_b.post(f"/retention/requests/{dr['id']}/approve", json={}),
        owner_b.get(f"/customers/{tenant_a.customer_id}/profile"),
        owner_b.post(f"/customers/{tenant_a.customer_id}/order-proposals"),
    ]
    assert [c.status_code for c in checks] == [404] * len(checks)
    assert owner_b.get("/orders").json() == [] and owner_b.get("/sync-conflicts").json() == []
    assert owner_b.get("/kpis").json()["metrics"]["quote_volume"]["value"] == 0
    with get_sessionmaker()() as db:  # RLS: tenant B session sees none of A's Stage 3 rows even without filters
        set_tenant(db, tenant_b.org_id, None)
        for table in (
            "orders",
            "order_proposals",
            "sync_conflicts",
            "sync_runs",
            "connector_connections",
            "deletion_requests",
        ):
            assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table  # noqa: S608
        with pytest.raises(Exception):  # noqa: B017 - WITH CHECK refuses an insert for another tenant
            db.add(
                Order(
                    organization_id=tenant_a.org_id,
                    number="SO-EVIL",
                    currency="AED",
                    status="proposed",
                    source="manual",
                )
            )
            db.flush()
        db.rollback()


def test_order_number_sequence_is_per_tenant(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        a1 = order_service.allocate_number(db, tenant_a.org_id)
        a2 = order_service.allocate_number(db, tenant_a.org_id)
        set_tenant(db, tenant_b.org_id, None)
        b1 = order_service.allocate_number(db, tenant_b.org_id)
        db.rollback()
    assert a1.endswith("00001") and a2.endswith("00002") and b1.endswith("00001")
