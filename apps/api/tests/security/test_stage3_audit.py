"""Security audit — Stage 3 (customer & order intelligence), part 1.

Tenant isolation / RLS of the ten Stage 3 tables and the order sequence, worker scope of ``deletion_requests``,
cross-tenant ids on every Stage 3 route, the authorization matrix of the five new permissions, data exposure
(costs, margins, exports, failed-rows CSV), formula-safe CSV, audit coverage and the retention workflow.

Findings are strict ``xfail`` tests carrying their finding id (SEC3-xx, see docs/evidence/security-stage3-2026-11-02.md);
passing checks are plain tests. Nothing here modifies product code.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_tenant, set_worker_scope
from app.models.audit import AuditEvent
from app.models.customer import Customer
from app.models.followup import JobRun
from app.models.intelligence import DeletionRequest, Order, SyncConflict, SyncRun
from app.models.quote import QuoteVersion
from app.services.intelligence import retention
from app.services.jobs import queue
from tests.conftest import Tenant, login_as, upload
from tests.integration.test_stage3_intelligence import _connector, approved_quote, audit_actions, seed_confirmed_orders

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
ROLES = ("owner", "admin", "sales", "approver", "readonly")


# ------------------------------------------------------------------ helpers
def _tenant_sql(org_id: uuid.UUID, sql: str, **params: object) -> list[tuple]:  # type: ignore[type-arg]
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = [tuple(r) for r in db.execute(text(sql), params).all()]
        db.commit()
    return rows


def _stage3_fixture(app, tenant: Tenant) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """One row in every Stage 3 table for ``tenant`` (orders+lines+events, proposal, stock, fx, connector, run,
    conflict, deletion request). Returns the ids needed for cross-tenant probes."""
    sales, approver, q = approved_quote(app, tenant)
    admin = login_as(app, tenant.users["admin"])
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    conn = _connector(admin, ["read:products", "read:stock", "read:orders", "read:customers"])
    run = admin.post(
        f"/connectors/{conn['id']}/sync",
        json={
            "entity": "products",
            "idempotency_key": "k-products",
            "records": [{"sku": "SKU-1001", "list_price": "9.99"}],
        },
    ).json()
    stock = admin.post(
        f"/connectors/{conn['id']}/sync",
        json={
            "entity": "stock",
            "idempotency_key": "k-stock",
            "records": [{"sku": "SKU-1001", "quantity": "77", "observed_at": utcnow().isoformat()}],
        },
    ).json()
    assert stock["applied"] == 1, stock
    conflict = admin.get("/sync-conflicts").json()[0]
    fx = admin.post(
        "/fx-rates", json={"base_currency": "USD", "quote_currency": "AED", "rate": "3.6725", "as_of": "2026-01-01"}
    ).json()
    seed_confirmed_orders(tenant, [40, 20])
    proposal = sales.post(f"/customers/{tenant.customer_id}/order-proposals").json()
    dr = sales.post("/retention/requests", json={"customer_id": str(tenant.customer_id), "reason": "erasure"}).json()
    imp = upload(
        admin,
        "/data/import/customers?idempotency_key=k-import",
        "c.csv",
        b"name,reference,email\r\nBad,CUST-1,x@y.example\r\n",
    ).json()
    return {
        "quote": q["id"],
        "order": order["id"],
        "connector": conn["id"],
        "run": run["id"],
        "import_run": imp["id"],
        "conflict": conflict["id"],
        "fx": fx["id"],
        "proposal": proposal["id"],
        "deletion": dr["id"],
        "customer": str(tenant.customer_id),
    }


# =============================================================== A. RLS / DB
def test_stage3_tables_force_rls_with_tenant_policy_and_app_role_grants(tenant_a: Tenant) -> None:
    with get_sessionmaker()() as db:
        for table in STAGE3_TABLES:
            rs, frs = db.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
            ).one()
            assert rs and frs, table
            pol = db.execute(
                text(
                    "SELECT qual, with_check FROM pg_policies WHERE tablename = :t AND policyname = 'tenant_isolation'"
                ),
                {"t": table},
            ).one()
            assert "app.current_org" in pol[0] and "app.current_org" in pol[1], table
            if table == "deletion_requests":
                assert "app.worker" in pol[0]
            else:
                assert "app.worker" not in pol[0], f"{table} must not carry the worker scope"
        # grants: the runtime role has only DML on the new tables, nothing more
        privs = {
            (t, p)
            for t, p in db.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = 'quotepilot_app' AND table_name = ANY(:tables)"
                ),
                {"tables": list(STAGE3_TABLES)},
            )
        }
        for table in STAGE3_TABLES:
            assert {p for t, p in privs if t == table} == {"SELECT", "INSERT", "UPDATE", "DELETE"}, table
        assert (
            db.execute(text("SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user")).scalar()
            is False
        )
        db.rollback()


def test_rls_blocks_every_stage3_table_for_the_other_tenant_even_with_raw_sql(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    _stage3_fixture(app, tenant_a)
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        own = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in STAGE3_TABLES}  # noqa: S608
        assert all(n >= 1 for n in own.values()), own  # the fixture really populated every table
        db.rollback()
        set_tenant(db, tenant_b.org_id, None)
        for t in STAGE3_TABLES:
            assert db.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t  # noqa: S608
            # UPDATE / DELETE without a service filter touch nothing of tenant A
            if t in ("order_lines", "order_events", "stock_snapshots", "fx_rates"):
                stmt = f"DELETE FROM {t} WHERE organization_id = :o"  # noqa: S608
            else:
                stmt = f"UPDATE {t} SET updated_at = now() WHERE organization_id = :o"  # noqa: S608
            assert db.execute(text(stmt), {"o": str(tenant_a.org_id)}).rowcount == 0, t
        db.rollback()
        # no tenant context at all → nothing visible
        set_tenant(db, None, None)
        for t in STAGE3_TABLES:
            assert db.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t  # noqa: S608
        db.rollback()


def test_with_check_refuses_inserting_rows_for_another_tenant_in_every_stage3_table(
    tenant_a: Tenant, tenant_b: Tenant
) -> None:
    a = str(tenant_a.org_id)
    inserts = {
        "orders": "INSERT INTO orders (id, organization_id, number, currency, status, source, subtotal, tax_total, grand_total) VALUES (gen_random_uuid(), :o, 'SO-X', 'AED', 'proposed', 'manual', 0, 0, 0)",
        "order_proposals": "INSERT INTO order_proposals (id, organization_id, customer_id, kind, status, lines, evidence, currency) VALUES (gen_random_uuid(), :o, :c, 'repeat', 'proposed', '[]', '[]', 'AED')",
        "fx_rates": "INSERT INTO fx_rates (id, organization_id, base_currency, quote_currency, rate, as_of, source, created_at) VALUES (gen_random_uuid(), :o, 'USD', 'AED', 1, now(), 'manual', now())",
        "connector_connections": "INSERT INTO connector_connections (id, organization_id, kind, name, scopes, status, config) VALUES (gen_random_uuid(), :o, 'csv', 'x', '[]', 'active', '{}')",
        "sync_runs": "INSERT INTO sync_runs (id, organization_id, kind, idempotency_key, entity, status, received, applied, conflicts, rejected, report) VALUES (gen_random_uuid(), :o, 'csv_import', 'k', 'customers', 'running', 0, 0, 0, 0, '{}')",
        "deletion_requests": "INSERT INTO deletion_requests (id, organization_id, subject_type, subject_id, reason, status, summary, legal_hold) VALUES (gen_random_uuid(), :o, 'customer', :c, 'r', 'requested', '{}', false)",
    }
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_b.org_id, None)
        for sql in inserts.values():
            with pytest.raises(Exception, match="row-level security"):
                db.execute(text(sql), {"o": a, "c": str(tenant_a.customer_id)})
            db.rollback()
            set_tenant(db, tenant_b.org_id, None)
        assert db.execute(text("SELECT count(*) FROM orders")).scalar() == 0
        db.rollback()


def test_order_sequence_update_is_scoped_to_the_callers_org_row(tenant_a: Tenant, tenant_b: Tenant) -> None:
    """``allocate_number`` runs ``UPDATE organizations`` as the app role. The app role holds a full UPDATE grant on
    ``organizations`` (0001), so the only barrier is RLS ``org_self``: tenant B must not be able to bump A's sequence."""
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_b.org_id, None)
        r = db.execute(
            text("UPDATE organizations SET order_seq = order_seq + 100 WHERE id = :o RETURNING order_seq"),
            {"o": str(tenant_a.org_id)},
        )
        assert r.rowcount == 0
        # ... nor can it move its own row to another organisation's id or read A's counter
        assert (
            db.execute(text("SELECT order_seq FROM organizations WHERE id = :o"), {"o": str(tenant_a.org_id)}).first()
            is None
        )
        db.rollback()
        set_tenant(db, tenant_a.org_id, None)
        assert (
            db.execute(text("SELECT order_seq FROM organizations WHERE id = :o"), {"o": str(tenant_a.org_id)}).scalar()
            == 0
        )
        db.rollback()


def test_worker_scope_is_never_set_by_the_api_and_purge_cannot_cross_tenants(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """``deletion_requests`` is additionally readable under ``app.worker=true`` for the scheduler tick. That flag is
    transaction-local, set only by worker code, and the purge handler itself runs under the job's tenant."""
    import inspect

    from app.api import deps
    from app.api.routers import intelligence as router_mod

    assert "set_worker_scope" not in inspect.getsource(router_mod) and "set_worker_scope" not in inspect.getsource(deps)
    ids = _stage3_fixture(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    assert owner.post(f"/retention/requests/{ids['deletion']}/approve", json={}).status_code == 200
    with get_sessionmaker()() as db:
        # the scheduler tick sees A's request under the worker flag ...
        set_worker_scope(db)
        assert db.execute(text("SELECT count(*) FROM deletion_requests")).scalar() == 1
        db.rollback()  # ... and the flag is gone with the transaction
        assert db.execute(text("SELECT current_setting('app.worker', true)")).scalar() in (None, "")
        assert db.execute(text("SELECT count(*) FROM deletion_requests")).scalar() == 0
        db.rollback()
        # a purge job run under tenant B's context cannot find (let alone purge) A's request
        set_tenant(db, tenant_b.org_id, None)
        from app.core.errors import NotFoundError
        from app.models.org import Organization

        org_b = db.get(Organization, tenant_b.org_id)
        assert org_b is not None
        with pytest.raises(NotFoundError):
            queue.HANDLERS["purge_customer"](db, org_b, {"request_id": ids["deletion"]})
        db.rollback()
        set_tenant(db, tenant_a.org_id, None)
        c = db.get(Customer, tenant_a.customer_id)
        assert c is not None and not c.name.startswith("Deleted customer")  # untouched
        db.rollback()


# ================================================= B. cross-tenant via the API
def test_every_stage3_route_returns_404_for_another_tenants_ids(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    ids = _stage3_fixture(app, tenant_a)
    b = login_as(app, tenant_b.users["owner"])
    attempts = [
        ("GET", f"/orders/{ids['order']}", None),
        ("POST", f"/orders/{ids['order']}/confirm", {}),
        ("POST", f"/orders/{ids['order']}/fulfil", {}),
        ("POST", f"/orders/{ids['order']}/cancel", {"reason": "pwn"}),
        ("POST", f"/quotes/{ids['quote']}/orders", {}),
        ("POST", f"/order-proposals/{ids['proposal']}/approve", {}),
        ("POST", f"/order-proposals/{ids['proposal']}/reject", {"reason": "pwn"}),
        ("POST", f"/customers/{ids['customer']}/order-proposals", None),
        ("GET", f"/customers/{ids['customer']}/profile", None),
        ("GET", f"/customers/{ids['customer']}/contacts", None),
        ("POST", f"/connectors/{ids['connector']}/sync", {"entity": "products", "idempotency_key": "z", "records": []}),
        ("POST", f"/connectors/{ids['connector']}/disable", None),
        ("POST", f"/sync-conflicts/{ids['conflict']}/resolve", {"resolution": "apply_remote"}),
        ("GET", f"/data/runs/{ids['run']}/failed.csv", None),
        ("GET", f"/data/runs/{ids['import_run']}/failed.csv", None),
        ("POST", "/retention/requests", {"customer_id": ids["customer"], "reason": "pwn"}),
        ("POST", f"/retention/requests/{ids['deletion']}/approve", {}),
        ("POST", f"/retention/requests/{ids['deletion']}/reject", {"reason": "pwn"}),
        ("POST", f"/retention/requests/{ids['deletion']}/legal-hold", {"hold": True}),
    ]
    for method, path, body in attempts:
        r = b.get(path) if method == "GET" else (b.post(path, json=body) if body is not None else b.post(path))
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    # list endpoints and filters never leak
    assert b.get(f"/orders?customer_id={ids['customer']}").json() == []
    assert b.get("/orders").json() == [] and b.get("/order-proposals").json() == []
    assert b.get("/connectors").json() == [] and b.get("/sync-conflicts?status=").json() == []
    assert b.get("/data/runs").json() == [] and b.get("/retention/requests").json() == []
    assert b.get("/fx-rates").json() == []
    # same SKU in both demo catalogs: A's stock snapshot must not surface for B
    levels = {s["sku"]: s for s in b.get("/stock").json()}
    assert levels["SKU-1001"]["status"] == "unknown" and levels["SKU-1001"]["quantity"] is None
    # A's FX table must not convert B's figures
    assert b.get("/kpis").json()["reporting_currency"] == "EUR"
    # A's deletion request / order / proposal are not affected by B's attempts
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        dr = db.get(DeletionRequest, uuid.UUID(ids["deletion"]))
        assert dr is not None and dr.status == "requested" and dr.legal_hold is False
        o = db.get(Order, uuid.UUID(ids["order"]))
        assert o is not None and o.status == "proposed"
        db.rollback()


def test_idempotency_keys_are_per_tenant(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Same key in two tenants → two independent runs; B's replay never returns A's run (uq is org+key)."""
    a = login_as(app, tenant_a.users["admin"])
    b = login_as(app, tenant_b.users["admin"])
    csv_in = b"name,reference\r\nAlpha Co,A-1\r\n"
    ra = upload(a, "/data/import/customers?idempotency_key=shared-key", "c.csv", csv_in).json()
    rb = upload(b, "/data/import/customers?idempotency_key=shared-key", "c.csv", csv_in).json()
    assert ra["id"] != rb["id"] and rb["applied"] == 1
    ca = _connector(a, ["read:products"])
    cb = _connector(b, ["read:products"])
    sa = a.post(
        f"/connectors/{ca['id']}/sync", json={"entity": "products", "idempotency_key": "sync-1", "records": []}
    ).json()
    sb = b.post(
        f"/connectors/{cb['id']}/sync", json={"entity": "products", "idempotency_key": "sync-1", "records": []}
    ).json()
    assert sa["id"] != sb["id"]
    assert {r["id"] for r in b.get("/data/runs").json()} == {rb["id"], sb["id"]}


# ======================================================== C. authorization
# (route, method, body, roles allowed) — every other role must get 403 and leave no state behind
def _matrix(ids: dict[str, str]) -> list[tuple[str, str, dict | None, set[str]]]:  # type: ignore[type-arg]
    ea = {"owner", "admin", "sales", "approver"}  # edit_quotes
    ao = {"owner", "admin", "approver"}  # approve_order / export_data
    oa = {"owner", "admin"}  # manage_connectors / import_data / manage_retention
    return [
        ("POST", f"/quotes/{ids['quote']}/orders", {}, ea),
        ("POST", f"/orders/{ids['order']}/confirm", {}, ao),
        ("POST", f"/orders/{ids['order']}/cancel", {"reason": "r"}, ao),
        ("POST", f"/orders/{ids['order']}/fulfil", {}, ea),
        ("POST", f"/customers/{ids['customer']}/order-proposals", None, ea),
        ("POST", f"/order-proposals/{ids['proposal']}/approve", {}, ao),
        ("POST", f"/order-proposals/{ids['proposal']}/reject", {"reason": "r"}, ao),
        (
            "POST",
            "/fx-rates",
            {"base_currency": "GBP", "quote_currency": "AED", "rate": "4.7", "as_of": "2026-01-01"},
            oa,
        ),
        ("GET", "/data/export/customers", None, ao),
        ("GET", "/data/export/orders", None, ao),
        ("GET", "/data/export/products", None, ao),
        ("GET", "/data/runs", None, oa),
        ("GET", f"/data/runs/{ids['import_run']}/failed.csv", None, oa),
        ("GET", "/connectors", None, oa),
        ("POST", "/connectors", {"kind": "csv", "name": "x", "scopes": ["read:stock"]}, oa),
        (
            "POST",
            f"/connectors/{ids['connector']}/sync",
            {"entity": "products", "idempotency_key": "m", "records": []},
            oa,
        ),
        ("POST", f"/connectors/{ids['connector']}/disable", None, oa),
        ("GET", "/sync-conflicts", None, oa),
        ("POST", f"/sync-conflicts/{ids['conflict']}/resolve", {"resolution": "keep_local"}, oa),
        ("GET", "/retention/requests", None, oa),
        ("POST", "/retention/requests", {"customer_id": ids["customer"], "reason": "reason"}, ea),
        ("POST", f"/retention/requests/{ids['deletion']}/approve", {}, oa),
        ("POST", f"/retention/requests/{ids['deletion']}/reject", {"reason": "r"}, oa),
        ("POST", f"/retention/requests/{ids['deletion']}/legal-hold", {"hold": True}, oa),
    ]


def test_authorization_matrix_denies_every_role_outside_the_permission(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ids = _stage3_fixture(app, tenant_a)
    clients = {role: login_as(app, tenant_a.users[role]) for role in ROLES}
    upload(clients["admin"], "/data/import/customers?idempotency_key=cnt", "c.csv", b"name\r\nX\r\n")
    for method, path, body, allowed in _matrix(ids):
        for role in ROLES:
            if role in allowed:
                continue
            c = clients[role]
            r = c.get(path) if method == "GET" else (c.post(path, json=body) if body is not None else c.post(path))
            assert r.status_code == 403, (role, method, path, r.status_code, r.text)
            assert r.json()["error"] == "forbidden" and "Traceback" not in r.text
    # import_data: multipart route
    for role in ("sales", "approver", "readonly"):
        assert (
            upload(clients[role], "/data/import/customers?idempotency_key=m2", "c.csv", b"name\r\nY\r\n").status_code
            == 403
        )
        assert (
            upload(
                clients[role],
                "/data/import/orders?idempotency_key=m3",
                "o.csv",
                b"order_ref,sku,quantity,unit_price\r\n",
            ).status_code
            == 403
        )
    # nothing was created by the denied attempts
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert db.scalar(select(SyncRun).where(SyncRun.idempotency_key.in_(("m", "m2", "m3")))) is None
        assert db.scalar(select(Customer).where(Customer.name == "Y")) is None
        dr = db.get(DeletionRequest, uuid.UUID(ids["deletion"]))
        assert dr is not None and dr.status == "requested" and dr.legal_hold is False
        o = db.get(Order, uuid.UUID(ids["order"]))
        assert o is not None and o.status == "proposed"
        db.rollback()


def test_separation_of_duties_on_orders_proposals_and_deletions(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    approver = login_as(app, tenant_a.users["approver"])
    # order proposed by an approver: same approver 403, another approver-capable user 200
    _, _, q = approved_quote(app, tenant_a)
    o = approver.post(f"/quotes/{q['id']}/orders", json={}).json()
    assert approver.post(f"/orders/{o['id']}/confirm", json={}).status_code == 403
    # ... cancel is not gated by SoD (explicit design: any approver may cancel) but still needs a reason
    assert approver.post(f"/orders/{o['id']}/cancel", json={}).status_code == 422
    # repeat-order proposal approved by X becomes an order *created by X* → X cannot also confirm it
    seed_confirmed_orders(tenant_a, [40, 20])
    p = approver.post(f"/customers/{tenant_a.customer_id}/order-proposals").json()
    conv = approver.post(f"/order-proposals/{p['id']}/approve", json={}).json()
    assert approver.post(f"/orders/{conv['order_id']}/confirm", json={}).status_code == 403
    assert admin.post(f"/orders/{conv['order_id']}/confirm", json={}).status_code == 200
    # deletion: the requesting admin cannot approve; another admin/owner can; sales never can
    c2 = admin.post("/customers", json={"name": "Second Co"}).json()
    dr = admin.post("/retention/requests", json={"customer_id": c2["id"], "reason": "erasure"}).json()
    assert admin.post(f"/retention/requests/{dr['id']}/approve", json={}).status_code == 403
    assert (
        admin.post(f"/retention/requests/{dr['id']}/reject", json={"reason": "no"}).status_code == 200
    )  # rejecting own is allowed
    dr2 = admin.post("/retention/requests", json={"customer_id": c2["id"], "reason": "erasure"}).json()
    assert owner.post(f"/retention/requests/{dr2['id']}/approve", json={}).status_code == 200
    # allow_self_approval lifts both gates (explicit organisation setting, owner-only)
    assert owner.patch("/org", json={"allow_self_approval": True}).status_code == 200
    _, _, q2 = approved_quote(app, tenant_a)
    o2 = approver.post(f"/quotes/{q2['id']}/orders", json={}).json()
    assert approver.post(f"/orders/{o2['id']}/confirm", json={}).status_code == 200
    c3 = admin.post("/customers", json={"name": "Third Co"}).json()
    dr3 = admin.post("/retention/requests", json={"customer_id": c3["id"], "reason": "erasure"}).json()
    assert admin.post(f"/retention/requests/{dr3['id']}/approve", json={}).status_code == 200


# ======================================================== D. data exposure
def test_costs_and_margins_are_hidden_from_sales_and_readonly_everywhere(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ids = _stage3_fixture(app, tenant_a)
    ro = login_as(app, tenant_a.users["readonly"])
    sales = login_as(app, tenant_a.users["sales"])
    appr = login_as(app, tenant_a.users["approver"])
    for c in (ro, sales):
        o = c.get(f"/orders/{ids['order']}")
        assert o.status_code == 200 and "cost_price" not in o.text and "margin" not in o.text
        assert all("cost" not in k for k in c.get("/orders").json()[0])
        prof = c.get(f"/customers/{ids['customer']}/profile").json()
        assert "avg_margin_pct" not in prof["stats"] and "cost" not in str(prof["orders"])
        kp = c.get("/kpis")
        assert kp.status_code == 200 and "margin" not in kp.text and "cost" not in kp.text
        assert c.get("/stock").status_code == 200 and "cost" not in c.get("/stock").text
    props = sales.get("/order-proposals").json()
    assert props and all("cost" not in str(p["lines"]) for p in props)  # proposal lines carry last paid price only
    # approver: cost visible on orders and exports (view_costs), consistent with Stage 1
    assert all("cost_price" in ln for ln in appr.get(f"/orders/{ids['order']}").json()["lines"])
    hdr = appr.get("/data/export/orders").content.decode("utf-8-sig").splitlines()[0]
    assert "cost_price" in hdr and "margin_pct" in hdr
    # export denial for sales/readonly is audited exactly once per attempt with role + entity
    before = len(audit_actions(tenant_a.org_id, "export.denied"))
    assert ro.get("/data/export/products").status_code == 403
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        ev = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "export.denied").order_by(AuditEvent.created_at.desc())
        ).first()
        assert (
            ev is not None
            and ev.summary == {"entity": "products", "role": "readonly"}
            and ev.actor_user_id == uuid.UUID(ro.user_id)
        )
        db.rollback()
    assert len(audit_actions(tenant_a.org_id, "export.denied")) == before + 1


def test_failed_rows_csv_and_exports_neutralise_formulas_in_every_reflected_column(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    # header *and* values are attacker-controlled; every one of them fails validation and is echoed back
    csv_in = (
        b"name,reference,email,default_currency,notes,=cmd|' /C calc'!A0\r\n"
        b'=HYPERLINK("http://evil"),+1+1,@SUM(1),-ZZZ,\t=1+1,"=2+2"\r\n'
        b'ok name,CUST-1,bad-email,AED,"\r+1",x\r\n'
    )
    r = upload(admin, "/data/import/customers?idempotency_key=f1", "c.csv", csv_in)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["applied"] == 0 and run["rejected"] == 2
    failed = admin.get(f"/data/runs/{run['id']}/failed.csv")
    assert failed.status_code == 200 and failed.headers["content-type"].startswith("text/csv")
    assert (
        failed.headers["content-disposition"].startswith("attachment;")
        and failed.headers["cache-control"] == "no-store"
    )
    body = failed.content.decode("utf-8-sig")
    cells = [c for line in body.split("\r\n") if line for c in line.split('","')]
    for cell in cells:
        inner = cell.strip('"')
        assert not inner or inner[0] not in "=+-@\t\r", (cell, body)
    assert "'=HYPERLINK" in body and "'=cmd|" in body and "'+1+1" in body and "'@SUM" in body and "'-ZZZ" in body
    # customers export: name/reference/notes created through the normal API with formula prefixes
    admin.post("/customers", json={"name": "@evil", "reference": "-REF", "notes": "+cmd|' /C calc'!A0"})
    exp = admin.get("/data/export/customers").content.decode("utf-8-sig")
    assert '"\'@evil"' in exp and '"\'-REF"' in exp and "\"'+cmd" in exp
    # orders export: sku / description / unit pushed by a connector
    conn = _connector(admin, ["read:orders"])
    admin.post(
        f"/connectors/{conn['id']}/sync",
        json={
            "entity": "orders",
            "idempotency_key": "f2",
            "records": [
                {
                    "external_ref": "=1+1",
                    "lines": [
                        {"sku": "=HYPERLINK(1)", "description": "@x", "unit": "-u", "quantity": 1, "unit_price": 1}
                    ],
                }
            ],
        },
    )
    exp = admin.get("/data/export/orders").content.decode("utf-8-sig")
    assert '"\'=HYPERLINK(1)"' in exp and '"\'@x"' in exp and '"\'-u"' in exp
    # products export header is fixed; description comes from the catalog import (Stage 1 neutralises on export)
    assert admin.get("/data/export/products").content.decode("utf-8-sig").startswith('"sku","description"')


# ============================================================ F. audit
def test_every_stage3_mutation_writes_an_audit_row_and_failed_mutations_write_none(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ids = _stage3_fixture(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    approver = login_as(app, tenant_a.users["approver"])
    assert admin.post(f"/connectors/{ids['connector']}/disable").status_code == 200
    assert admin.post(f"/sync-conflicts/{ids['conflict']}/resolve", json={"resolution": "dismiss"}).status_code == 200
    assert approver.post(f"/order-proposals/{ids['proposal']}/reject", json={"reason": "no"}).status_code == 200
    assert approver.post(f"/orders/{ids['order']}/cancel", json={"reason": "dup"}).status_code == 200
    assert admin.post(f"/retention/requests/{ids['deletion']}/legal-hold", json={"hold": True}).status_code == 200
    assert admin.post(f"/retention/requests/{ids['deletion']}/legal-hold", json={"hold": False}).status_code == 200
    # fix round 1 (SEC3-02): once the deletion is approved no new RFQ/quote can be filed for the customer, so the
    # quote used by the "failed mutations" part below is prepared before the approval
    sales = login_as(app, tenant_a.users["sales"])
    _, _, q = approved_quote(app, tenant_a)
    # fix round 2 (SEC3-12, orchestrator edit, declared): an order can no longer be started for a customer pending
    # erasure either, so the order used by the "failed mutations" part is proposed before the approval as well
    o = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    assert owner.post(f"/retention/requests/{ids['deletion']}/approve", json={}).status_code == 200
    expected = {
        "order.propose",
        "order.cancelled",
        "order_proposal.generate",
        "order_proposal.reject",
        "fx_rate.create",
        "import.customers",
        "connector.create",
        "connector.sync",
        "connector.disable",
        "sync_conflict.resolve",
        "retention.request",
        "retention.legal_hold",
        "retention.approve",
    }
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        rows = db.scalars(select(AuditEvent).where(AuditEvent.organization_id == tenant_a.org_id)).all()
        actions = {r.action for r in rows}
        assert expected <= actions, expected - actions
        for r in rows:
            if r.action in expected:
                assert r.actor_user_id is not None and r.request_id, r.action  # attributable
                assert "@" not in str(r.summary), r.action  # no e-mail addresses in audit summaries
        db.rollback()
    # failed mutations: state checks run before the audit row, nothing is written
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        base = db.scalar(
            select(AuditEvent.id).where(AuditEvent.action.like("order.%")).order_by(AuditEvent.created_at.desc())
        )
        db.rollback()
    assert approver.post(f"/orders/{o['id']}/cancel", json={}).status_code == 422  # reason missing
    assert sales.post(f"/orders/{o['id']}/confirm", json={}).status_code == 403
    assert approver.post(f"/orders/{o['id']}/fulfil", json={}).status_code == 409  # proposed → fulfilled not allowed
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        latest = db.scalar(
            select(AuditEvent.id).where(AuditEvent.action.like("order.%")).order_by(AuditEvent.created_at.desc())
        )
        assert latest == base
        order = db.get(Order, uuid.UUID(o["id"]))
        assert order is not None and order.status == "proposed" and [e.kind for e in order.events] == ["proposed"]
        assert db.scalar(select(AuditEvent).where(AuditEvent.organization_id == tenant_a.org_id).limit(1)) is not None
        db.rollback()


# ========================================================= G. retention
def test_soft_delete_hides_the_customer_from_lists_exports_and_imports(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    dr = sales.post("/retention/requests", json={"customer_id": cid, "reason": "erasure"}).json()
    assert owner.post(f"/retention/requests/{dr['id']}/approve", json={}).status_code == 200
    assert cid not in {c["id"] for c in sales.get("/customers").json()}
    assert cid not in {c["id"] for c in sales.get("/customers?q=Alpha").json()}
    assert "Alpha Trading Customer" not in admin.get("/data/export/customers").content.decode("utf-8-sig")
    # CSV order import can no longer attach orders to the deleted customer by reference
    r = upload(
        admin,
        "/data/import/orders?idempotency_key=sd1",
        "o.csv",
        b"order_ref,customer_reference,sku,quantity,unit_price\r\nPO-1,CUST-1,SKU-1001,1,1\r\n",
    ).json()
    assert r["applied"] == 0 and "unknown customer_reference" in str(r["report"])
    # repeat-order proposals refuse the deleted customer
    assert sales.post(f"/customers/{cid}/order-proposals").status_code == 404
    # a second deletion request is refused, the first one stays approved
    assert sales.post("/retention/requests", json={"customer_id": cid, "reason": "again"}).status_code == 409


def test_sec3_02_soft_deleted_customer_is_not_processed_any_further(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    sales.post(f"/customers/{cid}/contacts", json={"full_name": "Buyer", "email": "buyer@customer.example"})
    dr = sales.post("/retention/requests", json={"customer_id": cid, "reason": "erasure"}).json()
    assert owner.post(f"/retention/requests/{dr['id']}/approve", json={}).status_code == 200
    gaps: list[str] = []
    if sales.get(f"/customers/{cid}/profile").status_code != 404:
        gaps.append("profile (incl. contacts) still readable")
    if sales.get(f"/customers/{cid}/contacts").status_code != 404:
        gaps.append("contacts still readable")
    if (
        sales.post(f"/customers/{cid}/contacts", json={"full_name": "New", "email": "new@customer.example"}).status_code
        == 201
    ):
        gaps.append("new contact (PII) can be attached")
    if sales.patch(f"/customers/{cid}", json={"name": "Renamed", "email": "again@customer.example"}).status_code == 200:
        gaps.append("customer still editable")
    if sales.post("/rfqs", json={"text": "1 pcs SKU-1001", "customer_id": cid}).status_code == 201:
        gaps.append("new RFQ can be filed for the customer")
    if sales.post("/quotes", json={"customer_id": cid}).status_code == 201:
        gaps.append("new quote can be created for the customer")
    conn = _connector(admin, ["read:orders", "read:customers"])
    run = admin.post(
        f"/connectors/{conn['id']}/sync",
        json={
            "entity": "orders",
            "idempotency_key": "sd2",
            "records": [
                {
                    "external_ref": "E-1",
                    "customer_reference": "CUST-1",
                    "lines": [{"sku": "SKU-1001", "quantity": 1, "unit_price": 1}],
                }
            ],
        },
    ).json()
    if run["applied"] == 1:
        gaps.append("connector attaches new orders to the deleted customer")
    run2 = admin.post(
        f"/connectors/{conn['id']}/sync",
        json={
            "entity": "customers",
            "idempotency_key": "sd3",
            "records": [{"reference": "CUST-1", "name": "Alpha Trading Customer", "email": "erp@customer.example"}],
        },
    ).json()
    if run2["conflicts"] == 1:
        gaps.append("connector stages the deleted customer's data as a conflict")
    assert gaps == [], gaps


def test_sec3_03_purge_removes_pii_copies_outside_the_customer_row(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    email = "buyer@alpha-customer.example"
    admin.patch(f"/customers/{cid}", json={"name": "Alpha Trading Customer", "email": email})
    _, _, q = approved_quote(app, tenant_a)  # document snapshot carries the customer name
    conn = _connector(admin, ["read:customers"])
    admin.post(
        f"/connectors/{conn['id']}/sync",
        json={
            "entity": "customers",
            "idempotency_key": "p1",
            "records": [
                {"reference": "CUST-1", "name": "Alpha Trading Customer", "email": "other@alpha-customer.example"}
            ],
        },
    )
    upload(
        admin,
        "/data/import/customers?idempotency_key=p2",
        "c.csv",
        f"name,reference,email\r\nAlpha Trading Customer,CUST-1,{email}\r\n".encode(),
    )
    dr = sales.post("/retention/requests", json={"customer_id": cid, "reason": "erasure"}).json()
    assert owner.post(f"/retention/requests/{dr['id']}/approve", json={}).status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            text("UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id"), {"id": dr["id"]}
        )
        db.commit()
        assert retention.enqueue_due_purges(db) == 1 and queue.run_once(db, "w") == 1
        set_tenant(db, tenant_a.org_id, None)
        cust = db.get(Customer, tenant_a.customer_id)
        assert cust is not None and cust.email is None and cust.name.startswith("Deleted customer")
        leftovers: list[str] = []
        for c in db.scalars(select(SyncConflict)).all():
            if (
                email in str(c.local_value)
                or "Alpha Trading Customer" in str(c.local_value)
                or "alpha-customer" in str(c.remote_value)
            ):
                leftovers.append("sync_conflicts.local_value/remote_value")
        for r in db.scalars(select(SyncRun)).all():
            if email in str(r.report):
                leftovers.append("sync_runs.report.failed_rows")
        for v in db.scalars(select(QuoteVersion)).all():
            if "Alpha Trading Customer" in str(v.document_snapshot):
                leftovers.append("quote_versions.document_snapshot")
        db.rollback()
    assert leftovers == [], sorted(set(leftovers))


def test_purge_is_idempotent_and_blocked_by_legal_hold(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    dr = sales.post("/retention/requests", json={"customer_id": cid, "reason": "erasure"}).json()
    assert owner.post(f"/retention/requests/{dr['id']}/approve", json={}).status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            text("UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id"), {"id": dr["id"]}
        )
        db.commit()
        # a hold placed before the tick keeps the request out of the queue
        assert admin.post(f"/retention/requests/{dr['id']}/legal-hold", json={"hold": True}).status_code == 200
        assert retention.enqueue_due_purges(db) == 0
        assert admin.post(f"/retention/requests/{dr['id']}/legal-hold", json={"hold": False}).status_code == 200
        assert retention.enqueue_due_purges(db) == 1
        assert queue.run_once(db, "w") == 1
        assert retention.enqueue_due_purges(db) == 0 and queue.run_once(db, "w") == 0  # nothing to redo
        set_tenant(db, tenant_a.org_id, None)
        req = db.get(DeletionRequest, uuid.UUID(dr["id"]))
        assert req is not None and req.status == "executed"
        # replaying the handler is a no-op (job idempotency + status guard)
        from app.models.org import Organization

        org = db.get(Organization, tenant_a.org_id)
        assert org is not None
        queue.HANDLERS["purge_customer"](db, org, {"request_id": dr["id"]})
        assert db.scalars(select(AuditEvent).where(AuditEvent.action == "retention.purged")).all().__len__() == 1
        # legal hold can no longer be toggled on an executed request
        db.rollback()
    assert admin.post(f"/retention/requests/{dr['id']}/legal-hold", json={"hold": True}).status_code == 409


def test_sec3_01_purge_resumes_after_a_dead_lettered_job(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    dr = sales.post("/retention/requests", json={"customer_id": cid, "reason": "erasure"}).json()
    assert owner.post(f"/retention/requests/{dr['id']}/approve", json={}).status_code == 200
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            text("UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id"), {"id": dr["id"]}
        )
        db.commit()
        assert retention.enqueue_due_purges(db) == 1
        # hold arrives after the job was queued; every attempt fails until the job is dead
        assert admin.post(f"/retention/requests/{dr['id']}/legal-hold", json={"hold": True}).status_code == 200
        for _ in range(6):
            queue.run_once(db, "w")
            set_tenant(db, tenant_a.org_id, None)
            db.execute(
                text("UPDATE job_runs SET next_run_at = now() - interval '1 second' WHERE kind = 'purge_customer'")
            )
            db.commit()
        set_tenant(db, tenant_a.org_id, None)
        job = db.scalar(select(JobRun).where(JobRun.kind == "purge_customer"))
        assert job is not None and job.status == "dead", job.status
        db.rollback()
        # the hold is lifted → the retention workflow must still complete
        assert admin.post(f"/retention/requests/{dr['id']}/legal-hold", json={"hold": False}).status_code == 200
        for _ in range(3):
            retention.enqueue_due_purges(db)
            queue.run_once(db, "w")
        set_tenant(db, tenant_a.org_id, None)
        req = db.get(DeletionRequest, uuid.UUID(dr["id"]))
        assert req is not None and req.status == "executed", (req.status, req.legal_hold, req.purge_after)
        db.rollback()


def test_customer_profile_contacts_follow_the_stage2_contact_route_visibility(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Profiles embed contacts (PII). They must not widen visibility beyond ``GET /customers/{id}/contacts``."""
    sales = login_as(app, tenant_a.users["sales"])
    ro = login_as(app, tenant_a.users["readonly"])
    cid = str(tenant_a.customer_id)
    sales.post(
        f"/customers/{cid}/contacts",
        json={"full_name": "Buyer", "email": "buyer@customer.example", "phone": "+971-50-000"},
    )
    for c in (sales, ro):
        direct = c.get(f"/customers/{cid}/contacts")
        prof = c.get(f"/customers/{cid}/profile")
        assert direct.status_code == prof.status_code == 200
        assert {x["id"] for x in prof.json()["contacts"]} == {x["id"] for x in direct.json()}
        assert set(prof.json()["contacts"][0]) == set(direct.json()[0])  # same fields, no extra columns


def test_order_prices_are_copied_not_recomputed_and_totals_use_decimal(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    o = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        # tamper with the catalog after approval: the proposed order must still carry the approved document's prices
        db.execute(
            text("UPDATE products SET list_price = list_price * 10 WHERE organization_id = :o"),
            {"o": str(tenant_a.org_id)},
        )
        db.commit()
    c = approver.post(f"/orders/{o['id']}/confirm", json={}).json()
    assert c["grand_total"] == q["current_version"]["grand_total"]
    assert sum(Decimal(ln["line_total"]) for ln in c["lines"]) == Decimal(c["grand_total"])
