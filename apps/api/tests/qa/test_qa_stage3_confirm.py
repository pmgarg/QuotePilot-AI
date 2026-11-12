"""QA (independent) re-verification of Stage 3 after fix round 1.

Order totals against the approved quote document for several currencies / discount / tax shapes, and the
retention behaviour after approval and after the worker purge. Synthetic data only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.session import get_sessionmaker
from app.services.intelligence import retention
from app.services.jobs import queue
from tests.conftest import Tenant, login_as, make_tenant
from tests.integration.test_quotes import make_quote
from tests.qa.test_qa_stage3_helpers import (
    approved_quote,
    connector,
    csv_bytes,
    import_csv,
    run_sql,
    sync,
)

pytestmark = pytest.mark.integration

TWO_LINES = "1. 500 pcs SKU-1001 Hex bolt M8x40 zinc plated\n2. 333 pcs SKU-1003 Hex nut M8 zinc plated\n"


def _approved_with_discount_and_zero_tax(app, tenant: Tenant):  # type: ignore[no-untyped-def]
    """Line 1 gets a 7.5 % discount, line 2 a 0 % tax rate; returns (sales, approved quote json)."""
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id), TWO_LINES)
    lines = q["current_version"]["lines"]
    assert len(lines) == 2
    patched = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                {"id": lines[0]["id"], "discount_pct": "7.5"},
                {"id": lines[1]["id"], "tax_rate_pct": "0"},
            ],
        },
    )
    assert patched.status_code == 200, patched.text
    q2 = patched.json()
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q2["lock_version"]})
    assert s.status_code == 200, s.text
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert a.status_code == 200, a.text
    return sales, a.json()


@pytest.mark.parametrize(("currency", "places"), [("AED", 2), ("JPY", 0), ("KWD", 3), ("BHD", 3)])
def test_order_totals_equal_approved_document_for_discount_zero_tax_and_minor_units(  # type: ignore[no-untyped-def]
    app, currency: str, places: int
) -> None:
    t = make_tenant(f"Totals {currency}", currency=currency, tax="5", min_margin="0")
    sales, q = _approved_with_discount_and_zero_tax(app, t)
    v = q["current_version"]
    assert q["currency"] == currency
    assert Decimal(v["discount_total"]) > 0 and any(Decimal(ln["tax_rate_pct"]) == 0 for ln in v["lines"])
    r = sales.post(f"/quotes/{q['id']}/orders", json={})
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["currency"] == currency
    assert Decimal(order["subtotal"]) == Decimal(v["subtotal"]) - Decimal(v["discount_total"])
    assert Decimal(order["tax_total"]) == Decimal(v["tax_total"])
    assert Decimal(order["grand_total"]) == Decimal(v["grand_total"])
    assert Decimal(order["subtotal"]) + Decimal(order["tax_total"]) == Decimal(order["grand_total"])
    # every figure is expressed in the currency's minor units, not more
    for key in ("subtotal", "tax_total", "grand_total"):
        got = order[key].split(".")[1] if "." in order[key] else ""
        assert len(got) <= places, (currency, key, order[key])
    by_sku = {ln["sku"]: ln for ln in order["lines"]}
    for ln in v["lines"]:
        assert Decimal(by_sku[ln["sku"]]["line_total"]) == Decimal(ln["line_total"]), (currency, ln["sku"])


def test_no_header_level_discount_exists_on_the_quote_document(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """The data model has line discounts only (discount_total is their sum); there is nothing else to copy."""
    _, _, q = approved_quote(app, tenant_a)
    v = q["current_version"]
    assert not [k for k in v if "header" in k.lower()]
    assert not [k for k in q if "header" in k.lower()]


def _erasure_setup(app, tenant: Tenant):  # type: ignore[no-untyped-def]
    """Customer CUST-1 with: an open sync conflict, an import failed row, an approved quote with stored PDF, an order."""
    sales = login_as(app, tenant.users["sales"])
    admin = login_as(app, tenant.users["admin"])
    approver = login_as(app, tenant.users["approver"])
    conn = connector(admin, ["read:customers"])
    run = sync(
        admin,
        conn["id"],
        "customers",
        "c1",
        [{"reference": "CUST-1", "name": "Alpha Renamed By Erp", "email": "erp-secret@alpha.example"}],
    ).json()
    assert run["conflicts"] == 1
    bad = import_csv(
        admin,
        "customers",
        "imp1",
        csv_bytes(
            ["reference", "name", "email", "default_currency"],
            [["CUST-1", "Alpha Trading Customer", "failedrow@alpha.example", "ZZZ"]],
        ),
    )
    assert bad.status_code == 201 and bad.json()["rejected"] == 1, bad.text
    _, _, q = approved_quote(app, tenant, sales=sales, approver=approver)
    pdf = sales.post(f"/quotes/{q['id']}/versions/{q['current_version']['version_no']}/pdf")
    assert pdf.status_code == 200, pdf.text
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    return sales, admin, q, order


def _approve_erasure(sales, admin, cid: str) -> str:  # type: ignore[no-untyped-def]
    req = sales.post("/retention/requests", json={"customer_id": cid, "reason": "erasure requested"}).json()
    ap = admin.post(f"/retention/requests/{req['id']}/approve", json={})
    assert ap.status_code == 200, ap.text
    return str(req["id"])


def test_after_approval_customer_is_hidden_and_every_new_processing_route_refuses(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, order = _erasure_setup(app, tenant_a)
    cid = str(tenant_a.customer_id)
    _approve_erasure(sales, admin, cid)
    for ac in (sales, admin):
        assert cid not in {c["id"] for c in ac.get("/customers").json()}
    assert "CUST-1" not in admin.get("/data/export/customers").content.decode("utf-8-sig")
    owner = login_as(app, tenant_a.users["owner"])
    for ac in (sales, admin, owner):  # all roles, including manage_retention: nothing new may start
        assert ac.post("/rfqs", json={"text": "1 pcs SKU-1001", "customer_id": cid}).status_code == 404
        assert ac.post("/quotes", json={"customer_id": cid}).status_code == 404
        contact = ac.post(f"/customers/{cid}/contacts", json={"full_name": "New", "email": "new@alpha.example"})
        assert contact.status_code == 404
        assert ac.post(f"/customers/{cid}/order-proposals").status_code == 404
    # commercial records stay reachable
    assert sales.get(f"/quotes/{q['id']}").status_code == 200 and sales.get(f"/orders/{order['id']}").status_code == 200


def test_legal_hold_blocks_approval_and_a_due_purge_and_purge_resumes_after_release(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    cid = str(tenant_a.customer_id)
    req = sales.post("/retention/requests", json={"customer_id": cid, "reason": "erasure requested"}).json()
    assert admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": True}).status_code == 200
    assert admin.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 409
    admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": False})
    assert admin.post(f"/retention/requests/{req['id']}/approve", json={}).status_code == 200
    admin.post(f"/retention/requests/{req['id']}/legal-hold", json={"hold": True})
    run_sql(
        tenant_a.org_id,
        "UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id",
        {"id": req["id"]},
    )
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 0
        assert queue.run_once(db, "qa-worker") == 0
    name = run_sql(tenant_a.org_id, "SELECT name FROM customers WHERE id = :c", {"c": cid})[0][0]
    assert name == "Alpha Trading Customer"  # not purged while held


def test_worker_purge_pseudonymises_customer_and_redacts_copies_but_keeps_quotes_and_orders(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, order = _erasure_setup(app, tenant_a)
    cid = str(tenant_a.customer_id)
    before = run_sql(
        tenant_a.org_id,
        "SELECT pdf_storage_key, document_snapshot::text FROM quote_versions WHERE quote_id = :q",
        {"q": q["id"]},
    )
    assert before[0][0] and "Alpha Trading Customer" in before[0][1]
    totals_before = run_sql(tenant_a.org_id, "SELECT grand_total FROM orders WHERE id = :o", {"o": order["id"]})
    req_id = _approve_erasure(sales, admin, cid)
    run_sql(
        tenant_a.org_id,
        "UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id",
        {"id": req_id},
    )
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1
        assert queue.run_once(db, "qa-worker") == 1
    cust = run_sql(tenant_a.org_id, "SELECT name, email, reference, notes FROM customers WHERE id = :c", {"c": cid})[0]
    assert cust[0] == f"Deleted customer {cid[:8]}" and cust[1] is None and cust[2] is None and cust[3] is None
    conflicts = run_sql(
        tenant_a.org_id, "SELECT local_value::text, remote_value::text, entity_key, status FROM sync_conflicts"
    )
    assert conflicts and all(
        "erp-secret" not in c[0] + c[1] and "Alpha" not in c[0] + c[1] and c[2] != "CUST-1" and c[3] != "open"
        for c in conflicts
    ), conflicts
    reports = run_sql(tenant_a.org_id, "SELECT report::text FROM sync_runs WHERE kind = 'csv_import'")
    assert reports and all("failedrow@alpha.example" not in r[0] and "Alpha Trading" not in r[0] for r in reports)
    after = run_sql(
        tenant_a.org_id,
        "SELECT pdf_storage_key, pdf_sha256, document_snapshot::text FROM quote_versions WHERE quote_id = :q",
        {"q": q["id"]},
    )
    assert after[0][0] is None and after[0][1] is None
    assert "Alpha Trading Customer" not in after[0][2] and "Deleted customer" in after[0][2]
    # quotes and orders remain, with their commercial content
    assert sales.get(f"/quotes/{q['id']}").status_code == 200
    kept = sales.get(f"/orders/{order['id']}")
    assert kept.status_code == 200 and kept.json()["grand_total"] == order["grand_total"]
    assert run_sql(tenant_a.org_id, "SELECT grand_total FROM orders WHERE id = :o", {"o": order["id"]}) == totals_before
    # the PDF is re-rendered on demand from the pseudonymised snapshot
    dl = sales.get(f"/quotes/{q['id']}/versions/{q['current_version']['version_no']}/pdf")
    assert dl.status_code == 200 and dl.content.startswith(b"%PDF")
    assert b"Alpha Trading Customer" not in dl.content


def test_no_sync_run_report_keeps_the_erased_customers_identifiers(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """The purge nulls customers.reference; the customer's reference must not survive in run reports either."""
    sales, admin, _, _ = _erasure_setup(app, tenant_a)
    cid = str(tenant_a.customer_id)
    req_id = _approve_erasure(sales, admin, cid)
    run_sql(
        tenant_a.org_id,
        "UPDATE deletion_requests SET purge_after = now() - interval '1 day' WHERE id = :id",
        {"id": req_id},
    )
    with get_sessionmaker()() as db:
        assert retention.enqueue_due_purges(db) == 1
        assert queue.run_once(db, "qa-worker") == 1
    leaks = run_sql(
        tenant_a.org_id,
        "SELECT kind FROM sync_runs WHERE report::text ~* '(CUST-1|erp-secret|failedrow@|Alpha Trading|Alpha Renamed)'",
    )
    assert not leaks, f"sync_runs of kinds {[r[0] for r in leaks]} still carry the erased customer's identifiers"
