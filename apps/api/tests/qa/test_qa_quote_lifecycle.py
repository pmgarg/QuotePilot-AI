"""QA (independent): quote state machine, expiry/withdrawal, overrides, rounding and exports.

Derived from docs/PRD.md: S1-F11, S1-F12, S1-F13, S1-F14, S1-F15, S1-F16, S1-F17 and acceptance IDs
S1-A11, S1-A12, S1-A13, S1-A15, S1-A16, S1-A18. These tests are integration evidence (API + PostgreSQL + RLS).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import update

from app.db.session import get_sessionmaker, set_tenant
from app.models.quote import Quote
from tests.conftest import AuthedClient, Tenant, login_as, make_tenant
from tests.integration.test_quotes import make_quote

pytestmark = pytest.mark.integration

CSV_HEADER_PUBLIC = [
    "line_no",
    "sku",
    "description",
    "quantity",
    "unit",
    "unit_price",
    "discount_pct",
    "tax_rate_pct",
    "line_subtotal",
    "discount_amount",
    "tax_amount",
    "line_total",
]


def _set_valid_until(org_id: uuid.UUID, quote_id: str, when: date) -> None:
    """Simulate the passage of time: move the validity date directly in the database (same tenant context)."""
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        res = db.execute(update(Quote).where(Quote.id == uuid.UUID(quote_id)).values(valid_until=when))
        assert res.rowcount == 1  # type: ignore[attr-defined]
        db.commit()


def _approved_quote(app, tenant: Tenant) -> tuple[AuthedClient, AuthedClient, dict]:  # type: ignore[no-untyped-def,type-arg]
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    a = approver.post(
        f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"], "reason": "ok"}
    )
    assert a.status_code == 200, a.text
    return sales, approver, a.json()


def _pending_quote(app, tenant: Tenant) -> tuple[AuthedClient, AuthedClient, dict]:  # type: ignore[no-untyped-def,type-arg]
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    return sales, approver, s.json()


# ---------------------------------------------------------------------------------------------------------------
# S1-A15 / S1-F15 — expired and withdrawn quotes
# ---------------------------------------------------------------------------------------------------------------


def test_expired_approved_quote_is_reported_expired(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _approver, q = _approved_quote(app, tenant_a)
    _set_valid_until(tenant_a.org_id, q["id"], date.today() - timedelta(days=1))
    detail = sales.get(f"/quotes/{q['id']}").json()
    assert detail["effective_status"] == "expired"
    listed = next(x for x in sales.get("/quotes").json() if x["id"] == q["id"])
    assert listed["effective_status"] == "expired"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_expired_approved_quote_cannot_be_exported_as_current(app, tenant_a: Tenant, method: str) -> None:  # type: ignore[no-untyped-def]
    """S1-A15: 'Expired / withdrawn quote → approve/export-as-current rejected with 409 state error'."""
    sales, _approver, q = _approved_quote(app, tenant_a)
    _set_valid_until(tenant_a.org_id, q["id"], date.today() - timedelta(days=1))
    assert sales.get(f"/quotes/{q['id']}").json()["effective_status"] == "expired"
    path = f"/quotes/{q['id']}/versions/1/pdf"
    r = sales.get(path) if method == "GET" else sales.post(path)
    assert r.status_code == 409, (method, r.status_code, r.headers.get("content-type"))
    assert r.json()["error"] in ("invalid_state", "blocked")


def test_quote_that_expired_while_pending_cannot_be_approved(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, s = _pending_quote(app, tenant_a)
    _set_valid_until(tenant_a.org_id, s["id"], date.today() - timedelta(days=1))
    r = approver.post(f"/quotes/{s['id']}/approve", json={"expected_lock_version": s["lock_version"], "reason": "ok"})
    assert r.status_code == 409, r.text
    assert r.json()["error"] in ("invalid_state", "blocked")
    after = sales.get(f"/quotes/{s['id']}").json()
    assert after["status"] == "pending_approval" and after["current_version"]["status"] == "pending_approval"
    assert after["current_version"]["decisions"] == []


def test_expired_quote_new_version_resets_validity(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _approver, q = _approved_quote(app, tenant_a)
    _set_valid_until(tenant_a.org_id, q["id"], date.today() - timedelta(days=1))
    lv = sales.get(f"/quotes/{q['id']}").json()["lock_version"]
    r = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": lv})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft" and body["effective_status"] == "draft" and body["current_version_no"] == 2
    assert date.fromisoformat(body["valid_until"]) >= date.today()
    assert body["versions"][0]["status"] == "approved"  # the approved version is immutable history


@pytest.mark.parametrize("action", ["approve", "reject", "submit"])
def test_withdrawn_pending_quote_cannot_change_state(app, tenant_a: Tenant, action: str) -> None:  # type: ignore[no-untyped-def]
    """S1-F15: withdrawn quotes cannot be approved. A quote withdrawn while pending must stay withdrawn."""
    sales, approver, s = _pending_quote(app, tenant_a)
    w = approver.post(
        f"/quotes/{s['id']}/withdraw", json={"expected_lock_version": s["lock_version"], "reason": "customer cancelled"}
    )
    assert w.status_code == 200 and w.json()["status"] == "withdrawn"
    lv = w.json()["lock_version"]
    actor = sales if action == "submit" else approver
    r = actor.post(f"/quotes/{s['id']}/{action}", json={"expected_lock_version": lv, "reason": "trying anyway"})
    assert r.status_code == 409, (action, r.status_code, r.text)
    after = sales.get(f"/quotes/{s['id']}").json()
    assert after["status"] == "withdrawn", (action, after["status"])
    assert after["effective_status"] == "withdrawn"
    assert [d["decision"] for d in after["current_version"]["decisions"]] == ["withdrawn"]


def test_withdrawn_approved_quote_cannot_be_exported_as_csv_current(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S1-F15 says expired/withdrawn quotes cannot be 'exported as current'; the CSV is an export too."""
    sales, approver, q = _approved_quote(app, tenant_a)
    w = approver.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "x"})
    assert w.status_code == 200
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    r = sales.get(f"/quotes/{q['id']}/versions/1/csv")
    assert r.status_code == 409, (r.status_code, r.text[:200])


def test_new_version_from_withdrawn_quote(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = _approved_quote(app, tenant_a)
    w = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "re-quote"}
    ).json()
    assert w["status"] == "withdrawn" and w["withdrawn_reason"] == "re-quote"
    # a draft-only role may not withdraw but may start the new version
    r = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": w["lock_version"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft" and body["current_version_no"] == 2 and body["withdrawn_reason"] is None
    assert [v["status"] for v in body["versions"]] == ["approved", "draft"]
    assert len(body["versions"][1]["lines"]) == len(body["versions"][0]["lines"])
    assert body["versions"][1]["lines"][0]["unit_price"] == body["versions"][0]["lines"][0]["unit_price"]
    # the original approved version keeps its decision trail
    assert [d["decision"] for d in body["versions"][0]["decisions"]] == ["approved", "withdrawn"]


# ---------------------------------------------------------------------------------------------------------------
# S1-F14 — wrong-state transitions
# ---------------------------------------------------------------------------------------------------------------


def test_wrong_state_transitions_return_409(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    lv = q["lock_version"]
    body = {"expected_lock_version": lv, "reason": "some reason"}
    # draft: approve/reject/withdraw/new-version are all invalid (role is allowed, state is not)
    for action in ("approve", "reject", "withdraw"):
        r = approver.post(f"/quotes/{q['id']}/{action}", json=body)
        assert r.status_code == 409 and r.json()["error"] == "invalid_state", (action, r.text)
    r = sales.post(f"/quotes/{q['id']}/versions", json=body)
    assert r.status_code == 409 and r.json()["error"] == "invalid_state"
    assert sales.get(f"/quotes/{q['id']}").json()["lock_version"] == lv  # nothing moved
    # pending: a second submit and a new version are invalid
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": lv}).json()
    body = {"expected_lock_version": s["lock_version"], "reason": "some reason"}
    assert sales.post(f"/quotes/{q['id']}/submit", json=body).status_code == 409
    assert sales.post(f"/quotes/{q['id']}/versions", json=body).status_code == 409
    # approved: reject and a second approve are invalid
    a = approver.post(f"/quotes/{q['id']}/approve", json=body).json()
    body = {"expected_lock_version": a["lock_version"], "reason": "some reason"}
    assert approver.post(f"/quotes/{q['id']}/reject", json=body).status_code == 409
    assert approver.post(f"/quotes/{q['id']}/approve", json=body).status_code == 409
    # withdraw twice
    w = approver.post(f"/quotes/{q['id']}/withdraw", json=body)
    assert w.status_code == 200
    body = {"expected_lock_version": w.json()["lock_version"], "reason": "again"}
    assert approver.post(f"/quotes/{q['id']}/withdraw", json=body).status_code == 409


def test_rejected_version_can_be_edited_resubmitted_and_approved(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, s = _pending_quote(app, tenant_a)
    rj = approver.post(
        f"/quotes/{s['id']}/reject", json={"expected_lock_version": s["lock_version"], "reason": "fix discount"}
    ).json()
    assert rj["status"] == "draft"
    e = sales.patch(f"/quotes/{s['id']}", json={"expected_lock_version": rj["lock_version"], "notes": "fixed"})
    assert e.status_code == 200
    s2 = sales.post(f"/quotes/{s['id']}/submit", json={"expected_lock_version": e.json()["lock_version"]}).json()
    a = approver.post(f"/quotes/{s['id']}/approve", json={"expected_lock_version": s2["lock_version"]}).json()
    assert a["status"] == "approved"
    assert [d["decision"] for d in a["current_version"]["decisions"]] == ["rejected", "approved"]
    assert a["current_version"]["decisions"][0]["reason"] == "fix discount"


# ---------------------------------------------------------------------------------------------------------------
# S1-A16 — optimistic concurrency on decisions, not only on edits
# ---------------------------------------------------------------------------------------------------------------


def test_stale_lock_version_on_approve_returns_conflict(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, s = _pending_quote(app, tenant_a)
    r = approver.post(f"/quotes/{s['id']}/approve", json={"expected_lock_version": s["lock_version"] - 1})
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert r.json()["current_lock_version"] == s["lock_version"]
    assert sales.get(f"/quotes/{s['id']}").json()["status"] == "pending_approval"


def test_failed_validation_does_not_consume_lock_version_or_audit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S1-F18 atomicity: a rejected mutation leaves no partial state (lock, lines, audit)."""
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    before = sales.get(f"/quotes/{q['id']}/history").json()["audit"]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "notes": "should not persist",
            "lines": [{"description": "bad", "quantity": "1", "unit_price": "1", "is_custom": True, "discount_pct": "150"}],
        },
    )
    assert r.status_code == 422
    after = sales.get(f"/quotes/{q['id']}").json()
    assert after["lock_version"] == q["lock_version"]
    assert after["current_version"]["notes"] is None
    assert len(after["current_version"]["lines"]) == len(q["current_version"]["lines"])
    assert len(sales.get(f"/quotes/{q['id']}/history").json()["audit"]) == len(before)


# ---------------------------------------------------------------------------------------------------------------
# S1-F12 — blockers and privileged override
# ---------------------------------------------------------------------------------------------------------------


def test_submit_with_zero_lines_is_blocked(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    assert any(b["code"] == "no_lines" for b in q["blockers"])
    r = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert r.status_code == 409 and r.json()["error"] == "blocked"
    assert {b["code"] for b in r.json()["blockers"]} == {"no_lines"}
    assert sales.get(f"/quotes/{q['id']}").json()["status"] == "draft"


def test_custom_line_without_price_blocks_submit_until_priced(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"description": "Special bracket, powder coated", "quantity": "2", "is_custom": True}],
        },
    ).json()
    line = q["current_version"]["lines"][0]
    assert line["is_custom"] and line["unit_price"] is None and line["price_source"] == "none"
    assert line["line_total"] == "0"
    assert [b["code"] for b in q["blockers"]] == ["missing_price"] and q["blockers"][0]["line_no"] == 1
    r = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert r.status_code == 409 and r.json()["blockers"][0]["code"] == "missing_price"
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": line["id"], "unit_price": "10"}]},
    ).json()
    assert q["blockers"] == [] and q["current_version"]["lines"][0]["price_source"] == "manual"
    assert q["current_version"]["lines"][0]["line_total"] == "21"  # 2 × 10 + 5 % tax
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 200


def _below_margin_quote(app, tenant: Tenant) -> tuple[AuthedClient, AuthedClient, dict, dict]:  # type: ignore[no-untyped-def,type-arg]
    sales = login_as(app, tenant.users["sales"])
    admin = login_as(app, tenant.users["admin"])
    q = make_quote(sales, str(tenant.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.55"}]},
    ).json()
    assert [b["code"] for b in q["blockers"]] == ["below_min_margin"]
    r = admin.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/margin-override",
        json={"expected_lock_version": q["lock_version"], "reason": "strategic account"},
    )
    assert r.status_code == 200 and r.json()["blockers"] == []
    return sales, admin, r.json(), ln


def test_margin_override_is_audited_and_visible_to_sales(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, ln = _below_margin_quote(app, tenant_a)
    seen = sales.get(f"/quotes/{q['id']}").json()
    line = seen["current_version"]["lines"][0]
    assert line["margin_override_reason"] == "strategic account"
    assert "cost_price" not in line and "margin_pct" not in line  # S1-F20: no cost exposure to Sales
    hist = sales.get(f"/quotes/{q['id']}/history").json()
    ev = next(e for e in hist["audit"] if e["action"] == "quote.margin_override")
    assert ev["actor_user_id"] == admin.user_id and ev["entity_id"] == ln["id"]
    assert ev["summary"]["reason"] == "strategic account"


def test_margin_override_cleared_after_price_edit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, ln = _below_margin_quote(app, tenant_a)
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.56"}]},
    ).json()
    assert q["current_version"]["lines"][0]["margin_override_reason"] is None
    assert [b["code"] for b in q["blockers"]] == ["below_min_margin"]
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409
    # override again, then a discount edit also invalidates it
    r = admin.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/margin-override",
        json={"expected_lock_version": q["lock_version"], "reason": "second override"},
    ).json()
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": r["lock_version"], "lines": [{"id": ln["id"], "discount_pct": "1"}]},
    ).json()
    assert q["current_version"]["lines"][0]["margin_override_reason"] is None
    assert [b["code"] for b in q["blockers"]] == ["below_min_margin"]


def test_margin_override_survives_notes_only_edit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _admin, q, _ln = _below_margin_quote(app, tenant_a)
    q = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "notes": "call customer"}).json()
    assert q["current_version"]["lines"][0]["margin_override_reason"] == "strategic account"
    assert q["blockers"] == []


def test_margin_override_survives_save_with_unchanged_price(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S1-F12: the override is recorded and shown on the quote. A save that re-sends the same price and discount
    (exactly what the web editor does on every 'Save draft') is not a price edit and must not drop the override."""
    sales, _admin, q, ln = _below_margin_quote(app, tenant_a)
    saved = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "notes": "delivery in 2 weeks",
            "lines": [
                {
                    "id": ln["id"],
                    "product_id": saved["product_id"],
                    "sku": saved["sku"],
                    "description": saved["description"],
                    "quantity": saved["quantity"],
                    "unit": saved["unit"],
                    "unit_price": saved["unit_price"],
                    "discount_pct": saved["discount_pct"],
                    "tax_rate_pct": saved["tax_rate_pct"],
                    "is_custom": saved["is_custom"],
                }
            ],
        },
    ).json()
    line = q["current_version"]["lines"][0]
    assert line["unit_price"] == saved["unit_price"] and line["discount_pct"] == saved["discount_pct"]
    assert line["margin_override_reason"] == "strategic account", "override dropped although price did not change"
    assert q["blockers"] == []


def test_margin_override_not_possible_on_pending_version(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, admin, q, ln = _below_margin_quote(app, tenant_a)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    r = admin.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/margin-override",
        json={"expected_lock_version": s["lock_version"], "reason": "late override"},
    )
    assert r.status_code == 409 and r.json()["error"] == "invalid_state"


# ---------------------------------------------------------------------------------------------------------------
# S1-A11 / S1-A12 — currency, customer default currency and rounding mode
# ---------------------------------------------------------------------------------------------------------------


def test_customer_default_currency_drives_quote_currency_and_blocks_autofill(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    cust = sales.post("/customers", json={"name": "Euro Buyer", "default_currency": "EUR"}).json()
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt M8x40\n", "customer_id": cust["id"]}).json()
    assert rfq["currency"] == "EUR" and rfq["items"][0]["match_status"] == "exact"
    assert rfq["items"][0]["candidates"][0]["currency"] == "AED"
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201, q.text
    quote = q.json()
    assert quote["currency"] == "EUR" and quote["customer_id"] == cust["id"]
    line = quote["current_version"]["lines"][0]
    assert line["unit_price"] is None and line["price_source"] == "none"  # never invent a cross-currency price
    assert [b["code"] for b in quote["blockers"]] == ["missing_price"]
    assert quote["current_version"]["pricing_snapshot"]["currency"] == "EUR"
    # an explicit currency on the RFQ wins over the customer default and allows catalog pricing again
    rfq2 = sales.post(
        "/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt M8x40\n", "customer_id": cust["id"], "currency": "aed"}
    ).json()
    assert rfq2["currency"] == "AED"
    q2 = sales.post(f"/rfqs/{rfq2['id']}/generate-quote").json()
    assert q2["current_version"]["lines"][0]["price_source"] == "catalog"
    assert q2["current_version"]["lines"][0]["unit_price"] == "0.85"


def test_unsupported_currency_rejected_everywhere(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    assert sales.post("/quotes", json={"currency": "XXX"}).status_code == 422
    assert sales.post("/rfqs", json={"text": "1 pcs SKU-1001", "currency": "XXX"}).status_code == 422
    assert sales.post("/customers", json={"name": "C", "default_currency": "XX"}).status_code == 422


def test_half_even_rounding_mode_applies_per_org(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Bankers Ltd", min_margin="0", tax="0")
    owner = login_as(app, t.users["owner"])
    r = owner.patch("/org", json={"rounding_mode": "HALF_EVEN"})
    assert r.status_code == 200 and r.json()["rounding_mode"] == "HALF_EVEN"
    assert owner.patch("/org", json={"rounding_mode": "BANKERS"}).status_code == 422
    sales = login_as(app, t.users["sales"])
    lines = [
        {"description": "a", "quantity": "1", "unit_price": "2.125", "is_custom": True},
        {"description": "b", "quantity": "1", "unit_price": "2.135", "is_custom": True},
        {"description": "c", "quantity": "1", "unit_price": "2.125", "is_custom": True, "tax_rate_pct": "5"},
    ]
    q = sales.post("/quotes", json={}).json()
    assert q["current_version"]["pricing_snapshot"]["rounding_mode"] == "HALF_EVEN"
    q = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": lines}).json()
    subs = [ln["line_subtotal"] for ln in q["current_version"]["lines"]]
    assert subs == ["2.12", "2.14", "2.12"], subs  # ties go to the even neighbour
    # tax on 2.12 at 5 % = 0.106 → 0.11 (not a tie); total 2.23
    assert q["current_version"]["lines"][2]["tax_amount"] == "0.11"
    assert q["current_version"]["grand_total"] == "6.49"  # 2.12 + 2.14 + 2.23
    # same inputs under the default HALF_UP organisation
    sales_a = login_as(app, tenant_a.users["sales"])
    qa = sales_a.post("/quotes", json={}).json()
    qa = sales_a.patch(f"/quotes/{qa['id']}", json={"expected_lock_version": qa["lock_version"], "lines": lines[:2]}).json()
    assert [ln["line_subtotal"] for ln in qa["current_version"]["lines"]] == ["2.13", "2.14"]


def test_kwd_three_decimals_tax_and_discount(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Dinar Precision", currency="KWD", tax="10", min_margin="0")
    sales = login_as(app, t.users["sales"])
    q = sales.post("/quotes", json={}).json()
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"description": "x", "quantity": "3", "unit_price": "1.2345", "discount_pct": "2.5", "is_custom": True}],
        },
    ).json()
    ln = q["current_version"]["lines"][0]
    # 3 × 1.2345 = 3.7035 → 3.704 (3 dp, half up); discount 2.5 % = 0.0926 → 0.093; net 3.611; tax 0.3611 → 0.361
    assert (ln["line_subtotal"], ln["discount_amount"], ln["tax_amount"], ln["line_total"]) == (
        "3.704",
        "0.093",
        "0.361",
        "3.972",
    )


# ---------------------------------------------------------------------------------------------------------------
# S1-F16 / S1-F17 / S1-F20 — exports and history per role
# ---------------------------------------------------------------------------------------------------------------


def _csv_header(ac: AuthedClient, quote_id: str) -> list[str]:
    import csv
    import io

    r = ac.get(f"/quotes/{quote_id}/versions/1/csv")
    assert r.status_code == 200
    return next(csv.reader(io.StringIO(r.text.lstrip("\ufeff"))))


def test_csv_header_per_role(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _sales, _approver, q = _approved_quote(app, tenant_a)
    for role in ("sales", "readonly"):
        assert _csv_header(login_as(app, tenant_a.users[role]), q["id"]) == CSV_HEADER_PUBLIC, role
    for role in ("owner", "admin", "approver"):
        assert _csv_header(login_as(app, tenant_a.users[role]), q["id"]) == [*CSV_HEADER_PUBLIC, "cost_price"], role


def test_csv_neutralises_every_dangerous_prefix(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={}).json()
    payloads = ["+cmd|' /C calc'!A0", "@SUM(1+1)", "-2+3", "=1+1", "= HYPERLINK(1)"]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"description": p, "quantity": "1", "unit_price": "1", "is_custom": True} for p in payloads],
        },
    ).json()
    assert len(q["current_version"]["lines"]) == len(payloads)
    text = sales.get(f"/quotes/{q['id']}/versions/1/csv").text
    import csv
    import io

    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    descriptions = [r[2] for r in rows[1 : 1 + len(payloads)]]
    for d in descriptions:
        assert d.startswith("'"), d
        assert d[1] in "+@-=", d
    # numeric cells are not prefixed (quantity column)
    from decimal import Decimal

    assert Decimal(rows[1][3]) == 1 and not rows[1][3].startswith("'")


def test_history_visible_to_readonly_without_costs(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _sales, approver, q = _approved_quote(app, tenant_a)
    ro = login_as(app, tenant_a.users["readonly"])
    hist = ro.get(f"/quotes/{q['id']}/history")
    assert hist.status_code == 200
    body = hist.json()
    assert body["versions"][0]["decisions"][0]["decision"] == "approved"
    assert body["versions"][0]["decisions"][0]["reason"] == "ok"
    assert body["versions"][0]["decisions"][0]["decided_by"] == approver.user_id
    for ln in body["versions"][0]["lines"]:
        assert "cost_price" not in ln and "margin_pct" not in ln
    actions = [e["action"] for e in body["audit"]]
    assert {"quote.create", "quote.submit", "quote.approve"} <= set(actions)
    priv = approver.get(f"/quotes/{q['id']}/history").json()
    assert all("cost_price" in ln for ln in priv["versions"][0]["lines"])
    # GET /org also hides the margin floor from read-only but not from the approver
    assert "min_margin_pct" not in ro.get("/org").json()
    assert "min_margin_pct" in approver.get("/org").json()


def test_pdf_bytes_identical_across_users_and_calls(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S1-A18: same approved version exported twice (by different users) → identical bytes."""
    sales, approver, q = _approved_quote(app, tenant_a)
    ro = login_as(app, tenant_a.users["readonly"])
    bodies = [c.get(f"/quotes/{q['id']}/versions/1/pdf") for c in (sales, approver, ro, sales)]
    assert all(r.status_code == 200 and r.content.startswith(b"%PDF-") for r in bodies)
    hashes = {hashlib.sha256(r.content).hexdigest() for r in bodies}
    assert len(hashes) == 1
    assert bodies[0].headers["X-Content-SHA256"] == hashes.pop()
    meta = sales.post(f"/quotes/{q['id']}/versions/1/pdf").json()
    assert meta["sha256"] == hashlib.sha256(bodies[0].content).hexdigest() and meta["size"] == len(bodies[0].content)
    assert sales.get(f"/quotes/{q['id']}").json()["current_version"]["pdf_sha256"] == meta["sha256"]


def test_draft_pdf_is_marked_not_approved_and_not_stored(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    r = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")
    assert sales.get(f"/quotes/{q['id']}").json()["current_version"]["pdf_sha256"] is None


def test_quote_list_search_and_filters(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _approver, approved = _approved_quote(app, tenant_a)
    draft = make_quote(sales, str(tenant_a.customer_id))
    assert [x["id"] for x in sales.get("/quotes?status=approved").json()] == [approved["id"]]
    assert [x["id"] for x in sales.get("/quotes?status=draft").json()] == [draft["id"]]
    assert sales.get(f"/quotes?customer_id={tenant_a.customer_id}").json()
    assert sales.get(f"/quotes?customer_id={uuid.uuid4()}").json() == []
    assert sales.get("/quotes?customer_id=not-a-uuid").status_code == 422
    assert sales.get("/quotes?q=" + "a" * 101).status_code == 422
    # like-wildcards are literal: no quote number contains '%' or '_'
    assert sales.get("/quotes?q=%25").json() == []
    assert sales.get("/quotes?q=%5F").json() == []
    assert len(sales.get(f"/quotes?q={approved['number'][-5:]}").json()) == 1
