"""QA (independent) — S3-F02 quote → order conversion with approval gate.

Covers: role matrix for propose/confirm/fulfil/cancel, proposer ≠ confirmer (and the owner-controlled
self-approval switch), the order state machine, cancellation reasons, CSRF on Stage 3 mutations, cost/margin
visibility per role on order payloads (S3-A02) and Decimal exactness of order totals against the *approved quote
document* (money is copied, never recomputed differently — CLAUDE.md "No float money", PRD S3-F02).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import API, Tenant, login_as, make_tenant
from tests.integration.test_quotes import make_quote
from tests.qa.test_qa_stage3_helpers import ONE_LINE_RFQ, ONE_LINE_TOTAL, approved_quote, audit_rows, run_sql

pytestmark = pytest.mark.integration


def test_role_matrix_for_order_lifecycle(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    readonly = login_as(app, tenant_a.users["readonly"])
    admin = login_as(app, tenant_a.users["admin"])
    owner = login_as(app, tenant_a.users["owner"])
    # propose: readonly 403; sales 201
    assert readonly.post(f"/quotes/{q['id']}/orders", json={}).status_code == 403
    r = sales.post(f"/quotes/{q['id']}/orders", json={"notes": "PO-QA-1"})
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["status"] == "proposed" and order["created_by"] == sales.user_id and order["confirmed_by"] is None
    # confirm: sales/readonly 403; approver 200 (not the proposer)
    assert sales.post(f"/orders/{order['id']}/confirm", json={}).status_code == 403
    assert readonly.post(f"/orders/{order['id']}/confirm", json={}).status_code == 403
    # fulfil before confirmation is a state error, not a silent success
    assert sales.post(f"/orders/{order['id']}/fulfil", json={}).status_code == 409
    c = approver.post(f"/orders/{order['id']}/confirm", json={})
    assert c.status_code == 200 and c.json()["status"] == "confirmed"
    assert c.json()["confirmed_by"] == approver.user_id and c.json()["confirmed_at"]
    # fulfil: readonly 403; sales 200
    assert readonly.post(f"/orders/{order['id']}/fulfil", json={}).status_code == 403
    f = sales.post(f"/orders/{order['id']}/fulfil", json={})
    assert f.status_code == 200 and f.json()["status"] == "fulfilled"
    # terminal: nothing moves out of fulfilled
    assert owner.post(f"/orders/{order['id']}/cancel", json={"reason": "late"}).status_code == 409
    assert admin.post(f"/orders/{order['id']}/confirm", json={}).status_code == 409
    events = [(e["kind"], e["actor_user_id"]) for e in sales.get(f"/orders/{order['id']}").json()["events"]]
    assert events == [("proposed", sales.user_id), ("confirmed", approver.user_id), ("fulfilled", sales.user_id)]
    actions = [a["action"] for a in audit_rows(tenant_a.org_id, "order.")]
    assert actions == ["order.propose", "order.confirmed", "order.fulfilled"]


def test_cancel_requires_reason_and_frees_the_quote_for_a_new_order(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    first = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    assert sales.post(f"/quotes/{q['id']}/orders", json={}).status_code == 409  # one open order per quote
    assert sales.post(f"/orders/{first['id']}/cancel", json={"reason": "x"}).status_code == 403  # sales may not
    assert approver.post(f"/orders/{first['id']}/cancel", json={}).status_code == 422
    assert approver.post(f"/orders/{first['id']}/cancel", json={"reason": "   "}).status_code == 422
    c = approver.post(f"/orders/{first['id']}/cancel", json={"reason": "customer withdrew PO"})
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert c.json()["events"][-1]["detail"] == "customer withdrew PO"
    assert approver.post(f"/orders/{first['id']}/confirm", json={}).status_code == 409  # cancelled is terminal
    second = sales.post(f"/quotes/{q['id']}/orders", json={})
    assert second.status_code == 201 and second.json()["number"] != first["number"]
    numbers = sorted(o["number"] for o in sales.get("/orders").json())
    assert numbers == sorted([first["number"], second.json()["number"]]) and numbers[0].endswith("00001")
    assert sales.get("/orders?status=cancelled").json()[0]["id"] == first["id"]
    assert [o["id"] for o in sales.get("/orders?status=proposed").json()] == [second.json()["id"]]


def test_separation_of_duties_follows_the_org_self_approval_switch(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, approver, q = approved_quote(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    mine = approver.post(f"/quotes/{q['id']}/orders", json={}).json()
    assert approver.post(f"/orders/{mine['id']}/confirm", json={}).status_code == 403
    assert approver.post(f"/orders/{mine['id']}/confirm", json={}).json()["action"] == "approve_order"
    # only an owner may switch self-approval on (Stage 1 rule) — and then the same approver may confirm
    assert admin.patch("/org", json={"allow_self_approval": True}).status_code == 403
    assert owner.patch("/org", json={"allow_self_approval": True}).status_code == 200
    ok = approver.post(f"/orders/{mine['id']}/confirm", json={})
    assert ok.status_code == 200 and ok.json()["confirmed_by"] == approver.user_id


def test_only_approved_versions_convert_and_expired_quotes_are_refused(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    draft = make_quote(sales, str(tenant_a.customer_id), ONE_LINE_RFQ)
    assert sales.post(f"/quotes/{draft['id']}/orders", json={}).status_code == 409
    submitted = sales.post(f"/quotes/{draft['id']}/submit", json={"expected_lock_version": draft["lock_version"]})
    assert submitted.status_code == 200
    assert sales.post(f"/quotes/{draft['id']}/orders", json={}).status_code == 409  # submitted ≠ approved
    _, _, q = approved_quote(app, tenant_a)
    assert sales.post(f"/quotes/{q['id']}/orders", json={"version_no": 7}).status_code == 404  # no such version
    assert sales.post(f"/quotes/{q['id']}/orders", json={"version_no": 0}).status_code == 422
    run_sql(tenant_a.org_id, "UPDATE quotes SET valid_until = current_date - 1 WHERE id = :id", {"id": q["id"]})
    assert sales.get(f"/quotes/{q['id']}").json()["effective_status"] == "expired"
    r = sales.post(f"/quotes/{q['id']}/orders", json={})
    assert r.status_code == 409 and "expired" in r.text


def test_csrf_is_enforced_on_stage3_mutations(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    # approver's cookie jar without the CSRF header
    r = approver.client.post(f"{API}/orders/{order['id']}/confirm", json={})
    assert r.status_code == 403 and r.json()["action"] == "csrf"
    r = approver.client.post(
        f"{API}/retention/requests", json={"customer_id": str(tenant_a.customer_id), "reason": "x"}
    )
    assert r.status_code == 403 and r.json()["action"] == "csrf"
    r = approver.client.post(
        f"{API}/fx-rates", json={"base_currency": "USD", "quote_currency": "AED", "rate": "1", "as_of": "2026-01-01"}
    )
    assert r.status_code == 403
    # unauthenticated → 401
    assert client.get(f"{API}/orders").status_code == 401
    assert client.get(f"{API}/kpis").status_code == 401
    assert client.get(f"{API}/data/export/customers").status_code == 401
    assert sales.get(f"/orders/{order['id']}").json()["status"] == "proposed"  # nothing changed


def test_cost_price_never_in_order_payloads_for_sales_and_readonly(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    readonly = login_as(app, tenant_a.users["readonly"])
    owner = login_as(app, tenant_a.users["owner"])
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    for ac in (sales, readonly):
        detail = ac.get(f"/orders/{order['id']}").json()
        assert detail["lines"] and all("cost_price" not in ln and "margin_pct" not in ln for ln in detail["lines"])
        for summary in ac.get("/orders").json():
            assert "cost_price" not in summary and "margin" not in str(summary.keys())
        profile = ac.get(f"/customers/{tenant_a.customer_id}/profile").json()
        assert "avg_margin_pct" not in profile["stats"]
        assert all("cost_price" not in o for o in profile["orders"])
    for ac in (approver, owner):
        detail = ac.get(f"/orders/{order['id']}").json()
        assert all("cost_price" in ln for ln in detail["lines"])
        assert next(ln for ln in detail["lines"] if ln["sku"] == "SKU-1001")["cost_price"] == "0.52"


def test_order_totals_equal_the_approved_quote_document_without_discount(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    v = q["current_version"]
    assert Decimal(order["grand_total"]) == Decimal(v["grand_total"]) == ONE_LINE_TOTAL
    assert Decimal(order["subtotal"]) == Decimal("425") and Decimal(order["tax_total"]) == Decimal("21.25")
    assert order["lines"][0]["line_total"] == "446.25" and order["lines"][0]["unit_price"] == "0.85"
    assert order["quote_version_no"] == v["version_no"] and order["currency"] == q["currency"] == "AED"


def test_order_totals_equal_the_approved_quote_document_with_line_discount(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S3-F02: an order carries the approved document's amounts. The quote rounds discount per line
    (425.00 − 31.88 = 393.12 net, 19.66 tax, 412.78 total — the figures the customer approved)."""
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id), ONE_LINE_RFQ)
    line = q["current_version"]["lines"][0]
    q2 = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": line["id"], "discount_pct": "7.5"}]},
    )
    assert q2.status_code == 200, q2.text
    q2 = q2.json()
    assert q2["current_version"]["grand_total"] == "412.78"
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q2["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    order = sales.post(f"/quotes/{q['id']}/orders", json={})
    assert order.status_code == 201, order.text
    order = order.json()
    approved = a.json()["current_version"]
    assert Decimal(order["subtotal"]) == Decimal(approved["subtotal"]) - Decimal(approved["discount_total"]), (
        f"order net {order['subtotal']} vs approved net 393.12"
    )
    assert Decimal(order["tax_total"]) == Decimal(approved["tax_total"]), f"order tax {order['tax_total']} vs 19.66"
    assert Decimal(order["grand_total"]) == Decimal(approved["grand_total"]), (
        f"order grand total {order['grand_total']} differs from the approved document {approved['grand_total']}"
    )


def test_zero_minor_unit_currency_order_matches_quote(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Yen Orders", currency="JPY", tax="10", min_margin="0")
    sales, _, q = approved_quote(app, t, "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    # 10 × 0.85 = 8.5 → 9 (HALF_UP, 0 dp); tax 10 % = 0.9 → 1; grand total 10
    assert q["current_version"]["grand_total"] == "10" and order["grand_total"] == "10"
    assert order["currency"] == "JPY" and "." not in order["grand_total"]


def test_order_numbers_are_sequential_per_tenant_and_year(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales_a, _, qa = approved_quote(app, tenant_a)
    sales_b, _, qb = approved_quote(app, tenant_b, "5 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    a1 = sales_a.post(f"/quotes/{qa['id']}/orders", json={}).json()["number"]
    b1 = sales_b.post(f"/quotes/{qb['id']}/orders", json={}).json()["number"]
    assert a1 == b1  # both tenants start their own sequence
    assert a1.startswith("SO-") and a1.endswith("-00001") and len(a1.split("-")) == 3
