"""S1-A14 / X-01: no cross-tenant read, update, download or approve; RLS enforced even for raw SQL."""

import uuid

import pytest
from sqlalchemy import text

from app.db.session import get_sessionmaker, set_tenant
from tests.conftest import FIXTURES, Tenant, login_as
from tests.integration.test_quotes import make_quote

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_cross_tenant_objects_are_404(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    a_sales = login_as(app, tenant_a.users["sales"])
    a_appr = login_as(app, tenant_a.users["approver"])
    b_owner = login_as(app, tenant_b.users["owner"])
    rfq = a_sales.post(
        "/rfqs", json={"text": (FIXTURES / "rfq_clean.txt").read_text(), "customer_id": str(tenant_a.customer_id)}
    ).json()
    q = make_quote(a_sales, str(tenant_a.customer_id))
    s = a_sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a_appr.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    file_rfq = a_sales.post("/rfqs", json={"text": "1 pcs SKU-1001"}).json()
    item = rfq["items"][0]
    attempts = [
        ("GET", f"/quotes/{q['id']}", None),
        ("GET", f"/quotes/{q['id']}/history", None),
        ("GET", f"/quotes/{q['id']}/versions/1/pdf", None),
        ("GET", f"/quotes/{q['id']}/versions/1/csv", None),
        ("POST", f"/quotes/{q['id']}/versions/1/pdf", {}),
        ("PATCH", f"/quotes/{q['id']}", {"expected_lock_version": 0, "notes": "pwn"}),
        ("POST", f"/quotes/{q['id']}/approve", {"expected_lock_version": 0}),
        ("POST", f"/quotes/{q['id']}/withdraw", {"expected_lock_version": 0, "reason": "pwn"}),
        ("POST", f"/quotes/{q['id']}/versions", {"expected_lock_version": 0}),
        ("GET", f"/rfqs/{rfq['id']}", None),
        ("PATCH", f"/rfqs/{rfq['id']}/items/{item['id']}", {"quantity": "1"}),
        ("DELETE", f"/rfqs/{rfq['id']}/items/{item['id']}", None),
        ("POST", f"/rfqs/{rfq['id']}/generate-quote", {}),
        ("POST", f"/rfqs/{file_rfq['id']}/reparse", {}),
        ("GET", f"/customers/{tenant_a.customer_id}", None),
        ("PATCH", f"/customers/{tenant_a.customer_id}", {"name": "pwn"}),
        ("GET", f"/catalogs/versions/{tenant_a.catalog_version_id}/products", None),
        ("POST", f"/catalogs/versions/{tenant_a.catalog_version_id}/activate", {}),
    ]
    for method, path, body in attempts:
        if method == "GET":
            r = b_owner.get(path)
        elif method == "POST":
            r = b_owner.post(path, json=body)
        elif method == "PATCH":
            r = b_owner.patch(path, json=body)
        else:
            r = b_owner.delete(path)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    # file downloads from the other tenant
    src = a_sales.get(f"/rfqs/{rfq['id']}").json()
    uploaded = a_sales.post(
        "/rfqs", files={"file": ("x.csv", (FIXTURES / "rfq_clean.csv").read_bytes(), "text/csv")}
    ).json()
    assert b_owner.get(f"/files/{uploaded['source_file_id']}").status_code == 404
    assert a_sales.get(f"/files/{uploaded['source_file_id']}").status_code == 200
    # lists never leak
    assert all(x["id"] != q["id"] for x in b_owner.get("/quotes").json())
    assert b_owner.get("/rfqs").json() == [] or all(
        x["id"] not in (rfq["id"], src["id"]) for x in b_owner.get("/rfqs").json()
    )
    assert all(c["id"] != str(tenant_a.customer_id) for c in b_owner.get("/customers").json())
    # using another tenant's product id inside own RFQ is refused
    own_rfq = b_owner.post("/rfqs", json={"text": "1 pcs something"}).json()
    a_product = rfq["items"][0]["candidates"][0]["product_id"]
    r = b_owner.patch(
        f"/rfqs/{own_rfq['id']}/items/{own_rfq['items'][0]['id']}", json={"selected_product_id": a_product}
    )
    assert r.status_code == 404
    # ... and in a quote line
    own_q = b_owner.post("/quotes", json={}).json()
    r = b_owner.patch(
        f"/quotes/{own_q['id']}",
        json={"expected_lock_version": 0, "lines": [{"product_id": a_product, "quantity": "1"}]},
    )
    assert r.status_code == 404
    # ... and customer id from other tenant
    assert b_owner.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).status_code == 404


def test_rls_blocks_raw_sql_without_service_filters(tenant_a: Tenant, tenant_b: Tenant) -> None:
    """Defense in depth: even a query that forgets the organization filter sees only the current tenant."""
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        rows = db.execute(text("SELECT organization_id FROM customers")).all()
        assert rows and all(r[0] == tenant_a.org_id for r in rows)
        assert db.execute(text("SELECT count(*) FROM organizations")).scalar() == 1
        # update across tenants affects zero rows
        res = db.execute(
            text("UPDATE customers SET name = 'pwned' WHERE organization_id = :b"), {"b": str(tenant_b.org_id)}
        )
        assert res.rowcount == 0
        # insert for another tenant is refused by the WITH CHECK policy
        with pytest.raises(Exception, match="row-level security"):
            db.execute(
                text(
                    "INSERT INTO customers (id, organization_id, name, created_at, updated_at) VALUES (:id, :b, 'x', now(), now())"
                ),
                {"id": str(uuid.uuid4()), "b": str(tenant_b.org_id)},
            )
        db.rollback()
    with get_sessionmaker()() as db:
        # no tenant context at all → nothing visible
        assert db.execute(text("SELECT count(*) FROM customers")).scalar() == 0
        assert db.execute(text("SELECT count(*) FROM quotes")).scalar() == 0
        db.rollback()


def test_app_role_cannot_bypass_rls_or_touch_audit() -> None:
    with get_sessionmaker()() as db:
        assert db.execute(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar() is False
        assert db.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar() is False
        forced = db.execute(
            text(
                "SELECT bool_and(relforcerowsecurity) FROM pg_class WHERE relname IN ('quotes','customers','rfqs','products','uploaded_files','quote_versions')"
            )
        ).scalar()
        assert forced is True
        assert db.execute(text("SELECT has_table_privilege(current_user, 'audit_events', 'UPDATE')")).scalar() is False
        assert db.execute(text("SELECT has_table_privilege(current_user, 'audit_events', 'DELETE')")).scalar() is False
