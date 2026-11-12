"""Regression tests for independent release review 2 (N2-1, N2-2, L2-4)."""

import json

import pytest
from sqlalchemy import text

from app.db.session import get_sessionmaker, set_tenant
from tests.conftest import FIXTURES, Tenant, login_as, upload
from tests.integration.test_quotes import make_quote

pytestmark = [pytest.mark.integration, pytest.mark.security]

MAPPING = json.dumps(
    {"sku": "SKU", "description": "Description", "unit": "Unit", "currency": "Currency", "list_price": "List Price"}
)


def test_n2_1a_relinking_a_product_in_another_currency_never_keeps_the_old_catalog_price(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    # a USD-priced product in the ACTIVE catalog version (new version with a mixed-currency row, activated)
    csv = (FIXTURES.parent.parent / "apps/api/app/fixtures/data/demo_catalog.csv").read_bytes()
    csv += b"USD-1,Imported widget priced in dollars,EA,USD,9.99,5.00,10,2026-01-01,2027-12-31\n"
    v = upload(admin, "/catalogs/import", "mixed.csv", csv, {"mapping": MAPPING, "catalog_name": "Main"}).json()
    assert admin.post(f"/catalogs/versions/{v['id']}/activate").status_code == 200
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    usd = next(p for p in sales.get(f"/catalogs/versions/{v['id']}/products?q=USD-1").json()["items"])
    q2 = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "product_id": usd["id"]}]},
    ).json()
    line = q2["current_version"]["lines"][0]
    assert line["sku"] == "USD-1" and line["unit_price"] is None and line["price_source"] == "none"
    assert {b["code"] for b in q2["blockers"]} == {"missing_price"}
    assert (
        sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q2["lock_version"]}).status_code == 409
    )
    # typing a price makes it manual → needs privileged confirmation
    q3 = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q2["lock_version"], "lines": [{"id": ln["id"], "unit_price": "40"}]},
    ).json()
    assert q3["current_version"]["lines"][0]["price_source"] == "manual"
    assert "manual_price_unconfirmed" in {b["code"] for b in q3["blockers"]}


def test_n2_1b_products_from_inactive_catalog_versions_cannot_be_linked(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    cheap = upload(
        admin,
        "/catalogs/import",
        "cheap.csv",
        b"SKU,Description,Unit,Currency,List Price\nSKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.01\n",
        {"mapping": MAPPING, "catalog_name": "Main"},
    ).json()
    # imported but NOT activated: still listable, never a price authority
    bogus = sales.get(f"/catalogs/versions/{cheap['id']}/products").json()["items"][0]
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "product_id": bogus["id"]}]},
    )
    assert r.status_code == 422 and "catalog version this quote prices from" in r.text  # O2-4: message corrected
    q2 = sales.get(f"/quotes/{q['id']}").json()
    assert q2["current_version"]["lines"][0]["unit_price"] == "0.85"  # unchanged, atomic refusal
    # archived versions are refused as well
    v2 = upload(
        admin,
        "/catalogs/import",
        "c2.csv",
        b"SKU,Description,Unit,Currency,List Price\nSKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90\n",
        {"mapping": MAPPING, "catalog_name": "Main"},
    ).json()
    admin.post(f"/catalogs/versions/{v2['id']}/activate")
    old = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1002").json()["items"][0]
    q3 = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    r = sales.patch(
        f"/quotes/{q3['id']}",
        json={
            "expected_lock_version": q3["lock_version"],
            "lines": [{"description": "x", "quantity": "1", "product_id": old["id"]}],
        },
    )
    assert r.status_code == 422


def _approve(app, tenant: Tenant, sales, q):  # type: ignore[no-untyped-def]
    approver = login_as(app, tenant.users["approver"])
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    return approver, a.json()


def test_n2_2_withdrawal_covers_every_approved_version_and_survives_new_versions(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _approve(app, tenant_a, sales, q)
    q = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    q = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "notes": "v2"}).json()
    approver, q = _approve(app, tenant_a, sales, q)
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 200
    w = owner.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "customer cancelled"}
    ).json()
    assert w["status"] == "withdrawn" and [v["status"] for v in w["versions"]] == ["withdrawn", "withdrawn"]
    for n in (1, 2):
        assert sales.get(f"/quotes/{q['id']}/versions/{n}/pdf").status_code == 409
        assert sales.get(f"/quotes/{q['id']}/versions/{n}/csv").status_code == 409
    # a new draft version (v3) must not revive v1 or v2
    v3 = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": w["lock_version"]}).json()
    assert v3["status"] == "draft" and v3["current_version_no"] == 3
    for n in (1, 2):
        assert sales.get(f"/quotes/{q['id']}/versions/{n}/pdf").status_code == 409
        assert sales.get(f"/quotes/{q['id']}/versions/{n}/csv").status_code == 409
    assert len([d for v in v3["versions"] for d in v["decisions"] if d["decision"] == "withdrawn"]) == 2


def test_n2_2_withdraw_while_a_draft_is_open_withdraws_the_approved_document(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _approve(app, tenant_a, sales, q)
    q = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    assert q["status"] == "draft" and sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 200
    w = approver.post(
        f"/quotes/{q['id']}/withdraw",
        json={"expected_lock_version": q["lock_version"], "reason": "pricing error in v1"},
    )
    assert w.status_code == 200, w.text
    body = w.json()
    assert body["status"] == "draft" and body["withdrawn_reason"] == "pricing error in v1"  # the draft stays editable
    assert [v["status"] for v in body["versions"]] == ["withdrawn", "draft"]
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    # nothing to withdraw on a plain draft
    q2 = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    assert (
        approver.post(
            f"/quotes/{q2['id']}/withdraw", json={"expected_lock_version": q2["lock_version"], "reason": "x"}
        ).status_code
        == 409
    )


def test_l2_4_session_and_user_identity_columns_are_immutable_even_for_the_owner_role(tenant_a: Tenant) -> None:
    import os

    from sqlalchemy import create_engine

    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True)
    with eng.connect() as conn:
        with pytest.raises(Exception, match="identity columns are immutable"):
            conn.execute(
                text("UPDATE users SET email = 'x@y.example' WHERE email = :e"), {"e": tenant_a.users["sales"]}
            )
        conn.rollback()
    eng.dispose()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        assert (
            db.execute(
                text("SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_sessions_identity','trg_users_identity')")
            ).scalar()
            == 2
        )
        db.rollback()


def test_r3_10_pending_ad_hoc_quote_with_a_stale_catalog_line_cannot_be_approved(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Release review 3 (R3-10): a catalog line that left the pricing version blocks approval too, not only submit."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    old = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001").json()["items"][0]
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"quantity": "1", "product_id": old["id"]}]},
    ).json()
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    csv_bytes = b"SKU,Description,Unit,Currency,List Price\nSKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90\n"
    mapping = '{"sku":"SKU","description":"Description","unit":"Unit","currency":"Currency","list_price":"List Price"}'
    up = admin.post(
        "/catalogs/import",
        files={"file": ("c2.csv", csv_bytes, "text/csv")},
        data={"mapping": mapping, "catalog_name": "Main"},
    )
    assert up.status_code == 201, up.text
    assert admin.post(f"/catalogs/versions/{up.json()['id']}/activate").status_code == 200
    r = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert r.status_code == 409 and "catalog_version_mismatch" in r.text
