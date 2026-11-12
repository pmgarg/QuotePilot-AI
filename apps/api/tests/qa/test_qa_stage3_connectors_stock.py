"""QA (independent) — S3-F06 connectors (declared scopes, idempotent sync, conflict queue, no silent overwrite;
S3-A04) and S3-F07 stock claims (timestamped sources only, fresh/stale/unknown labels; S3-A05)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.base import utcnow
from tests.conftest import Tenant, login_as, make_tenant
from tests.qa.test_qa_stage3_helpers import audit_rows, connector, run_sql, sync

pytestmark = pytest.mark.integration


def test_scopes_kinds_and_roles_are_enforced_on_connectors(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    for role in ("sales", "approver", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        assert ac.get("/connectors").status_code == 403, role
        assert ac.get("/sync-conflicts").status_code == 403, role
        assert ac.post("/connectors", json={"kind": "csv", "name": "x", "scopes": ["read:stock"]}).status_code == 403
    assert admin.post("/connectors", json={"kind": "sap", "name": "x", "scopes": ["read:stock"]}).status_code == 422
    assert admin.post("/connectors", json={"kind": "erp_stub", "name": "x", "scopes": []}).status_code == 422
    assert (
        admin.post("/connectors", json={"kind": "erp_stub", "name": "x", "scopes": ["write:orders"]}).status_code == 422
    )
    conn = connector(admin, ["read:stock", "read:stock"])
    assert conn["scopes"] == ["read:stock"] and conn["status"] == "active"
    csv_conn = connector(admin, ["read:customers"], kind="csv", name="Drop folder")  # one per kind
    assert admin.post("/connectors", json={"kind": "csv", "name": "y", "scopes": ["read:stock"]}).status_code == 409
    # entity outside the declared scopes → 403 and *no run recorded*
    for entity in ("products", "orders", "customers"):
        r = sync(admin, conn["id"], entity, f"k-{entity}", [])
        assert r.status_code == 403 and r.json()["action"] == f"read:{entity}", entity
    assert sync(admin, conn["id"], "invoices", "k-x", []).status_code == 422
    assert sync(admin, csv_conn["id"], "stock", "k-y", []).status_code == 403
    # disable → syncs refused, status visible, audited
    d = admin.post(f"/connectors/{conn['id']}/disable")
    assert d.status_code == 200 and d.json()["status"] == "disabled"
    assert sync(admin, conn["id"], "stock", "k-after", []).status_code == 409
    assert {c["id"]: c["status"] for c in admin.get("/connectors").json()}[conn["id"]] == "disabled"
    assert [a["action"] for a in audit_rows(tenant_a.org_id, "connector.")] == [
        "connector.create",
        "connector.create",
        "connector.disable",
    ]


def test_sync_is_idempotent_per_key_even_when_the_payload_changes(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = connector(admin, ["read:stock"])
    now = utcnow()
    ts = (now - timedelta(hours=1)).isoformat()
    first = sync(admin, conn["id"], "stock", "same", [{"sku": "SKU-1001", "quantity": "10", "observed_at": ts}]).json()
    replay = sync(admin, conn["id"], "stock", "same", [{"sku": "SKU-1001", "quantity": "999", "observed_at": ts}])
    assert replay.status_code == 201 and replay.json()["id"] == first["id"]
    assert {s["sku"]: s["quantity"] for s in admin.get("/stock").json()}["SKU-1001"] == "10"
    assert len([a for a in audit_rows(tenant_a.org_id, "connector.sync")]) == 1
    assert sync(admin, conn["id"], "stock", "", []).status_code == 422
    assert sync(admin, conn["id"], "stock", "x" * 161, []).status_code == 422
    too_many = [{"sku": "SKU-1001", "quantity": "1", "observed_at": ts}] * 5001
    assert sync(admin, conn["id"], "stock", "many", too_many).status_code in (413, 422)


def test_customer_master_data_differences_wait_in_the_queue(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    conn = connector(admin, ["read:customers"])
    original = sales.get(f"/customers/{tenant_a.customer_id}").json()
    assert original["reference"] == "CUST-1"
    records = [
        {"reference": "CUST-1", "name": "Alpha Trading Customer (renamed by ERP)", "email": "erp@alpha.example"},
        {"reference": "CUST-1-SAME", "name": "Brand New Co", "default_currency": "EUR"},
        {"reference": "", "name": "No ref"},
        {"reference": "BAD-CUR", "name": "Bad", "default_currency": "ZZZ"},
    ]
    run = sync(admin, conn["id"], "customers", "c1", records).json()
    assert (run["applied"], run["conflicts"], run["rejected"], run["status"]) == (1, 1, 2, "partial")
    assert sorted(r["reason"] for r in run["report"]["rejected"]) == [
        "reference and name required",
        "unsupported currency ZZZ",
    ]
    # nothing was overwritten
    assert sales.get(f"/customers/{tenant_a.customer_id}").json()["name"] == original["name"]
    assert "Brand New Co" in {c["name"] for c in sales.get("/customers").json()}
    (c,) = admin.get("/sync-conflicts").json()
    assert (c["entity"], c["entity_key"], sorted(c["fields"])) == ("customers", "CUST-1", ["email", "name"])
    assert c["local_value"]["name"] == original["name"] and c["remote_value"]["email"] == "erp@alpha.example"
    # keep_local: data unchanged; the same difference re-syncs into a *new* open conflict
    k = admin.post(f"/sync-conflicts/{c['id']}/resolve", json={"resolution": "keep_local", "note": "we are master"})
    assert k.status_code == 200 and k.json()["status"] == "resolved_local" and k.json()["resolved_by"] == admin.user_id
    assert sales.get(f"/customers/{tenant_a.customer_id}").json()["name"] == original["name"]
    assert admin.get("/sync-conflicts").json() == []
    run2 = sync(admin, conn["id"], "customers", "c2", records[:1]).json()
    assert run2["conflicts"] == 1
    (c2,) = admin.get("/sync-conflicts").json()
    assert c2["id"] != c["id"]
    # dismiss leaves data alone as well; invalid resolution refused
    assert admin.post(f"/sync-conflicts/{c2['id']}/resolve", json={"resolution": "overwrite"}).status_code == 422
    assert (
        admin.post(f"/sync-conflicts/{c2['id']}/resolve", json={"resolution": "dismiss"}).json()["status"]
        == "dismissed"
    )
    assert sales.get(f"/customers/{tenant_a.customer_id}").json()["email"] == original["email"]
    # apply_remote is the only path that changes master data, and it is audited with the fields
    sync(admin, conn["id"], "customers", "c3", records[:1])
    (c3,) = admin.get("/sync-conflicts").json()
    a = admin.post(f"/sync-conflicts/{c3['id']}/resolve", json={"resolution": "apply_remote"})
    assert a.status_code == 200 and a.json()["status"] == "resolved_remote"
    now = sales.get(f"/customers/{tenant_a.customer_id}").json()
    assert now["name"] == "Alpha Trading Customer (renamed by ERP)" and now["email"] == "erp@alpha.example"
    resolved = audit_rows(tenant_a.org_id, "sync_conflict.resolve")
    assert [r["summary"]["resolution"] for r in resolved] == ["keep_local", "dismiss", "apply_remote"]
    assert sorted(resolved[-1]["summary"]["fields"]) == ["email", "name"]
    assert {c["status"] for c in admin.get("/sync-conflicts?status=").json()} == {
        "resolved_local",
        "dismissed",
        "resolved_remote",
    }
    assert admin.get("/sync-conflicts?status=resolved_remote").json()[0]["id"] == c3["id"]


def test_product_cost_difference_is_a_conflict_not_visible_to_sales(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    conn = connector(admin, ["read:products"])
    run = sync(
        admin,
        conn["id"],
        "products",
        "p1",
        [
            {"sku": "sku-1001", "cost_price": "0.60"},  # SKU match is case-insensitive
            {"sku": "SKU-1002", "list_price": "-1"},  # invalid
            {"sku": "SKU-1003", "list_price": "0.250", "unit": "EA", "description": "Hex nut M8 zinc plated"},
        ],
    ).json()
    assert (run["applied"], run["conflicts"], run["rejected"]) == (1, 1, 1)
    (c,) = admin.get("/sync-conflicts").json()
    assert (
        c["fields"] == ["cost_price"]
        and c["local_value"]["cost_price"] == "0.520000"
        and c["remote_value"] == {"cost_price": "0.60"}
    )
    assert sales.get("/sync-conflicts").status_code == 403
    assert sales.get(f"/sync-conflicts/{c['id']}").status_code in (403, 404, 405)
    # active catalog untouched until a human applies it
    cost = run_sql(tenant_a.org_id, "SELECT cost_price FROM products WHERE sku = 'SKU-1001'")[0][0]
    assert str(cost) == "0.520000"
    assert admin.post(f"/sync-conflicts/{c['id']}/resolve", json={"resolution": "apply_remote"}).status_code == 200
    assert str(run_sql(tenant_a.org_id, "SELECT cost_price FROM products WHERE sku = 'SKU-1001'")[0][0]) == "0.600000"
    # historical quote lines keep their copied cost (documented: no retroactive repricing)
    assert admin.get("/data/export/products").status_code == 200


def test_stock_labels_follow_source_timestamps_not_sync_order(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    readonly = login_as(app, tenant_a.users["readonly"])
    assert {s["status"] for s in readonly.get("/stock").json()} == {"unknown"}  # catalog "Stock" column is no claim
    assert all(
        s["quantity"] is None and s["observed_at"] is None and s["source"] is None
        for s in readonly.get("/stock").json()
    )
    conn = connector(admin, ["read:stock"])
    now = utcnow()
    fresh_ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")  # Zulu suffix accepted
    older_ts = (now - timedelta(days=3)).isoformat()
    first = sync(
        admin, conn["id"], "stock", "s1", [{"sku": "SKU-1001", "quantity": "120", "observed_at": fresh_ts}]
    ).json()
    assert first["applied"] == 1
    # a later sync carrying an *older* observation must not regress the label
    second = sync(
        admin, conn["id"], "stock", "s2", [{"sku": "SKU-1001", "quantity": "5", "observed_at": older_ts}]
    ).json()
    assert second["applied"] == 1
    levels = {s["sku"]: s for s in readonly.get("/stock?q=SKU-1001").json()}
    assert levels["SKU-1001"]["status"] == "fresh" and levels["SKU-1001"]["quantity"] == "120"
    assert levels["SKU-1001"]["source"] == "erp_stub" and 0.9 <= levels["SKU-1001"]["age_hours"] <= 1.2
    # boundary around the configured window (24 h): 23.5 h → fresh, 24.5 h → stale; zero stock is a valid claim
    recs = [
        {"sku": "SKU-1002", "quantity": "7", "observed_at": (now - timedelta(hours=23, minutes=30)).isoformat()},
        {"sku": "SKU-1003", "quantity": "7", "observed_at": (now - timedelta(hours=24, minutes=30)).isoformat()},
        {"sku": "SKU-1004", "quantity": "0", "observed_at": fresh_ts},
        {"sku": "SKU-1005", "quantity": "-1", "observed_at": fresh_ts},
        {"sku": "SKU-1005", "quantity": "abc", "observed_at": fresh_ts},
        {"sku": "SKU-1005", "quantity": "1", "observed_at": "yesterday"},
        {"sku": "SKU-1005", "quantity": "1", "observed_at": (now + timedelta(minutes=5)).isoformat()},
        {"sku": "NOPE-1", "quantity": "1", "observed_at": fresh_ts},
    ]
    run = sync(admin, conn["id"], "stock", "s3", recs).json()
    assert (run["applied"], run["rejected"], run["status"]) == (3, 5, "partial")
    reasons = [r["reason"] for r in run["report"]["rejected"]]
    assert sum("observed_at" in r for r in reasons) == 2 and sum("future" in r for r in reasons) == 1
    levels = {s["sku"]: s for s in readonly.get("/stock").json()}
    assert levels["SKU-1002"]["status"] == "fresh" and levels["SKU-1003"]["status"] == "stale"
    assert levels["SKU-1004"]["status"] == "fresh" and levels["SKU-1004"]["quantity"] == "0"
    assert levels["SKU-1005"]["status"] == "unknown" and levels["SKU-1005"]["quantity"] is None
    assert "NOPE-1" not in levels
    # search escapes LIKE wildcards; a tenant without an active catalog reports nothing rather than guessing
    assert readonly.get("/stock?q=%").json() == []
    assert len(readonly.get("/stock?q=sku-100").json()) == 5
    bare = make_tenant("No Catalog Co", with_catalog=False)
    assert login_as(app, bare.users["owner"]).get("/stock").json() == []


def test_csv_connector_source_is_labelled_and_quote_editor_still_sees_no_invented_stock(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    conn = connector(admin, ["read:stock"], kind="csv", name="Warehouse CSV drop")
    ts = (utcnow() - timedelta(hours=2)).isoformat()
    assert (
        sync(admin, conn["id"], "stock", "c1", [{"sku": "SKU-3002", "quantity": "4", "observed_at": ts}]).json()[
            "applied"
        ]
        == 1
    )
    level = next(s for s in admin.get("/stock").json() if s["sku"] == "SKU-3002")
    assert level["source"] == "csv" and level["status"] == "fresh" and level["quantity"] == "4"
    assert "connector.sync" in [a["action"] for a in audit_rows(tenant_a.org_id, "connector.")]
