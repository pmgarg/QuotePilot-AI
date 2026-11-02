"""QA (independent) re-verification of release review 2 items on HEAD 58976f3 (round 4 = 80aaf7f).

Derived from docs/evidence/release-review-2-stage1-2026-11-01.md (N2-1 A/B, N2-2, L2-1, L2-5), docs/PRD.md
(S1-F11, S1-F12, S1-F15, S1-F16, S1-A11, S1-A15, S1-A18) and docs/API_CONTRACT.md (withdraw: "withdraws EVERY
approved version ... allowed while a newer draft is open"). Every probe here differs from the implementer's
regression tests in tests/integration/test_review2_fixes.py (other-currency product on a USD quote, editor-style
full re-sends, RFQ review path, archived-version pinning, multi-line atomicity, four-role export matrices through a
later approved version, pending-while-withdrawn, role/lock/reason matrix, migration 0005 data fix, R-2/R-3/R-4
re-checks). Integration evidence (API + PostgreSQL + RLS through the ASGI TestClient).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import API_DIR, FIXTURES, Tenant, login_as, make_tenant, upload
from tests.integration.test_quotes import make_quote

pytestmark = pytest.mark.integration

ROLES = ("sales", "approver", "readonly", "owner")
MAPPING = json.dumps(
    {
        "sku": "SKU",
        "description": "Description",
        "unit": "Unit",
        "currency": "Currency",
        "list_price": "List Price",
        "cost_price": "Cost Price",
    }
)
HEADER = b"SKU,Description,Unit,Currency,List Price,Cost Price\n"


# ---------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------


def _import(admin, name: str, rows: bytes, *, activate: bool) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = upload(admin, "/catalogs/import", name, HEADER + rows, {"mapping": MAPPING, "catalog_name": "Main"})
    assert r.status_code == 201, r.text
    v = r.json()
    if activate:
        a = admin.post(f"/catalogs/versions/{v['id']}/activate")
        assert a.status_code == 200, a.text
    return v  # type: ignore[no-any-return]


def _product(ac, version_id: str, sku: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    items = ac.get(f"/catalogs/versions/{version_id}/products?q={sku}").json()["items"]
    return next(p for p in items if p["sku"] == sku)  # type: ignore[no-any-return]


def _editor_payload(line: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Exactly what apps/web's quote editor sends for a line on every save (all fields, ids kept)."""
    base = {
        "id": line["id"],
        "product_id": line.get("product_id"),
        "sku": line.get("sku"),
        "description": line["description"],
        "quantity": line["quantity"],
        "unit": line["unit"],
        "unit_price": line["unit_price"],
        "discount_pct": line["discount_pct"],
        "tax_rate_pct": line["tax_rate_pct"],
        "is_custom": line["is_custom"],
    }
    base.update(changes)
    return base


def _approve(app, tenant: Tenant, sales, q):  # type: ignore[no-untyped-def]
    approver = login_as(app, tenant.users["approver"])
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert a.status_code == 200, a.text
    return approver, a.json()


def _new_version(sales, q, **patch: Any):  # type: ignore[no-untyped-def]
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]})
    assert nv.status_code == 201, nv.text
    q = nv.json()
    if patch:
        p = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], **patch})
        assert p.status_code == 200, p.text
        q = p.json()
    return q


def _export_status(ac, quote_id: str, version_no: int) -> tuple[int, int, int]:  # type: ignore[no-untyped-def]
    return (
        ac.get(f"/quotes/{quote_id}/versions/{version_no}/pdf").status_code,
        ac.post(f"/quotes/{quote_id}/versions/{version_no}/pdf").status_code,
        ac.get(f"/quotes/{quote_id}/versions/{version_no}/csv").status_code,
    )


def _assert_blocked_for_every_role(app, tenant: Tenant, quote_id: str, version_nos: list[int]) -> None:  # type: ignore[no-untyped-def]
    for role in ROLES:
        c = login_as(app, tenant.users[role])
        for n in version_nos:
            got = _export_status(c, quote_id, n)
            assert got == (409, 409, 409), (role, n, got)
            body = c.get(f"/quotes/{quote_id}/versions/{n}/pdf").json()
            assert body["error"] == "invalid_state", body


def _assert_exportable_for_every_role(app, tenant: Tenant, quote_id: str, version_no: int) -> bytes:  # type: ignore[no-untyped-def]
    first: bytes | None = None
    for role in ROLES:
        c = login_as(app, tenant.users[role])
        assert _export_status(c, quote_id, version_no) == (200, 200, 200), role
        data = c.get(f"/quotes/{quote_id}/versions/{version_no}/pdf").content
        first = first or data
        assert data == first, role  # S1-A18: the same approved document for every role
    assert first is not None
    return first


def _pdf_text(data: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _walk(obj: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            found.extend(_walk(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_walk(v, key))
    return found


def _codes(q: dict[str, Any]) -> set[str]:
    return {b["code"] for b in q["blockers"]}


# ---------------------------------------------------------------------------------------------------------------
# N2-1 A — re-linking to a product priced in another currency (S1-F11, S1-A11, S1-F12)
# ---------------------------------------------------------------------------------------------------------------


def test_n2_1a_usd_quote_linking_an_active_aed_product_carries_no_price_or_cost_and_needs_confirmation(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """The mirror image of the implementer's probe: the quote is in USD, the product (active version) in AED.
    No catalog price, no cost-based margin, submission blocked; a typed price is manual and needs owner/admin."""
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    approver = login_as(app, tenant_a.users["approver"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id), "currency": "USD"})
    assert q.status_code == 201, q.text
    q = q.json()
    assert q["currency"] == "USD"
    bolt = _product(sales, str(tenant_a.catalog_version_id), "SKU-1001")
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"description": "bolts", "quantity": "10", "product_id": bolt["id"]}],
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    ln = q["current_version"]["lines"][0]
    assert ln["sku"] == "SKU-1001" and ln["product_id"] == bolt["id"]
    assert ln["unit_price"] is None and ln["price_source"] == "none" and ln["is_custom"] is False
    assert _codes(q) == {"missing_price"}
    # no cross-currency cost or margin is carried into the quote (even for privileged viewers)
    priv = owner.get(f"/quotes/{q['id']}").json()["current_version"]["lines"][0]
    assert priv["cost_price"] is None and priv["margin_pct"] is None
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409
    # typed price → manual → privileged confirmation (approver may not clear it)
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "2.50"}]},
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["current_version"]["lines"][0]["price_source"] == "manual"
    assert _codes(q) == {"manual_price_unconfirmed"}
    denied = approver.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/price-override",
        json={"expected_lock_version": q["lock_version"], "reason": "approver says ok"},
    )
    assert denied.status_code == 403, denied.text
    ok = owner.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/price-override",
        json={"expected_lock_version": q["lock_version"], "reason": "USD price agreed with customer"},
    )
    assert ok.status_code == 200, ok.text
    q = ok.json()
    assert q["blockers"] == []
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 200


def test_editor_style_resave_of_an_untouched_catalog_line_keeps_the_catalog_price_and_raises_no_blocker(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """apps/web's quote editor re-sends every field of every line on "Save draft" (`apps/web/src/app/(app)/quotes/
    [id]/page.tsx:88`, `unit_price: l.unit_price`). Saving a quantity or notes change must not turn a catalog price
    into a `manual` one that then needs an owner/admin confirmation (S1-F11 deterministic pricing; S1-F12 blockers
    only for real problems; S1-A20 the Sales journey)."""
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    lines = q["current_version"]["lines"]
    assert all(ln["price_source"] == "catalog" for ln in lines) and q["blockers"] == []
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "notes": "delivery in two weeks",
            "lines": [_editor_payload(lines[0], quantity="11"), *[_editor_payload(ln) for ln in lines[1:]]],
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["current_version"]["lines"][0]["quantity"] == "11"
    assert [ln["price_source"] for ln in q["current_version"]["lines"]] == ["catalog"] * len(lines)
    assert [ln["unit_price"] for ln in q["current_version"]["lines"]] == [ln["unit_price"] for ln in lines]
    assert q["blockers"] == []
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 200


def test_n2_1a_editor_style_full_resend_relink_to_other_currency_product_never_yields_a_catalog_price(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """A re-link to a USD product on an AED quote through the editor's payload shape (stale unit_price still in the
    payload) must end as a *manual* (confirmable) price, never `catalog`, with no cost carried over; re-linking back
    to the AED product with no typed price restores the catalog price."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    mixed = _import(
        admin,
        "mixed.csv",
        b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.85,0.52\nUSD-9,Dollar priced gasket,EA,USD,9.99,4.00\n",
        activate=True,
    )
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    assert ln["price_source"] == "catalog" and ln["unit_price"] == "0.85"
    usd = _product(sales, mixed["id"], "USD-9")
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [_editor_payload(ln, product_id=usd["id"])]},
    )
    assert r.status_code == 200, r.text
    q = r.json()
    ln = q["current_version"]["lines"][0]
    assert ln["sku"] == "USD-9" and ln["product_id"] == usd["id"] and ln["price_source"] != "catalog"
    assert ln["price_source"] == "manual" and ln["unit_price"] == "0.85"
    assert "manual_price_unconfirmed" in _codes(q)
    priv = owner.get(f"/quotes/{q['id']}").json()["current_version"]["lines"][0]
    assert priv["cost_price"] is None and priv["margin_pct"] is None
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409
    # re-link back to the AED bolt with no typed price → catalog price, nothing to confirm
    bolt = _product(sales, mixed["id"], "SKU-1001")
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [_editor_payload(ln, product_id=bolt["id"], unit_price=None)],
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    ln = q["current_version"]["lines"][0]
    assert ln["sku"] == "SKU-1001" and ln["price_source"] == "catalog" and ln["unit_price"] == "0.85"
    assert q["blockers"] == []


# ---------------------------------------------------------------------------------------------------------------
# N2-1 B — only the quote's pricing catalog version is a price authority (S1-F11, S1-F04)
# ---------------------------------------------------------------------------------------------------------------


def test_n2_1b_rfq_review_cannot_select_a_product_from_an_imported_but_inactive_version(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Second entry point beside the editor: the RFQ review screen's candidate selection."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    cheap = _import(admin, "cheap.csv", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.01,0.01\n", activate=False)
    bogus = _product(sales, cheap["id"], "SKU-1001")
    rfq = sales.post(
        "/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", "customer_id": str(tenant_a.customer_id)}
    ).json()
    item = rfq["items"][0]
    assert item["selected_product_id"] not in (None, bogus["id"])
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": bogus["id"]})
    assert r.status_code == 422, r.text
    again = sales.get(f"/rfqs/{rfq['id']}").json()["items"][0]
    assert again["selected_product_id"] == item["selected_product_id"]
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201, q.text
    ln = q.json()["current_version"]["lines"][0]
    assert ln["unit_price"] == "0.85" and ln["price_source"] == "catalog"


def test_n2_1b_archived_version_product_is_refused_atomically_and_a_foreign_product_is_404(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """A quote without an RFQ pin prices from whatever version is active. After a new activation: the archived
    product is refused, the refusal is atomic (other line changes and the lock are untouched), the new active
    product is accepted at its new price, and another tenant's product id is a 404 that changes nothing."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    v1 = str(tenant_a.catalog_version_id)
    bolt_v1 = _product(sales, v1, "SKU-1001")
    nut_v1 = _product(sales, v1, "SKU-1003")
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                {"description": "bolts", "quantity": "10", "product_id": bolt_v1["id"]},
                {"description": "hand made bracket", "quantity": "1", "unit_price": "3", "is_custom": True},
            ],
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["current_version"]["lines"][0]["unit_price"] == "0.85"
    v2 = _import(admin, "v2.csv", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90,0.52\n", activate=True)
    # (a) a new line linking the archived nut → 422; the quantity change on line 1 in the same save is dropped too
    lines = q["current_version"]["lines"]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                _editor_payload(lines[0], quantity="99"),
                _editor_payload(lines[1]),
                {"description": "nuts", "quantity": "5", "product_id": nut_v1["id"]},
            ],
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "validation_error" and "catalog version" in r.text
    after = sales.get(f"/quotes/{q['id']}").json()
    assert after["lock_version"] == q["lock_version"]
    assert [ln["quantity"] for ln in after["current_version"]["lines"]] == ["10", "1"]
    assert len(after["current_version"]["lines"]) == 2
    # (b) another tenant's product id: 404, nothing changed, nothing leaked
    foreign = login_as(app, tenant_b.users["sales"])
    foreign_bolt = _product(foreign, str(tenant_b.catalog_version_id), "SKU-1001")
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [_editor_payload(lines[0], product_id=foreign_bolt["id"]), _editor_payload(lines[1])],
        },
    )
    assert r.status_code == 404, r.text
    assert sales.get(f"/quotes/{q['id']}").json()["lock_version"] == q["lock_version"]
    # (c) the new active version's product is accepted at its own price
    bolt_v2 = _product(sales, v2["id"], "SKU-1001")
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                _editor_payload(lines[0], product_id=bolt_v2["id"], unit_price=None),
                _editor_payload(lines[1]),
            ],
        },
    )
    assert r.status_code == 200, r.text
    ln = r.json()["current_version"]["lines"][0]
    assert ln["product_id"] == bolt_v2["id"] and Decimal(ln["unit_price"]) == Decimal("0.90")
    assert ln["price_source"] == "catalog"


def test_n2_1b_unpinned_draft_still_saves_after_a_catalog_activation_when_lines_are_resent_by_the_editor(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """Regression guard for the new check: the editor re-sends `product_id` for *every* line on every save. A draft
    whose lines were linked while v1 was active must remain saveable (quantity/notes edits) after v2 is activated,
    and its untouched lines must keep their price. If this fails, every such draft becomes read-only in the UI."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    bolt_v1 = _product(sales, str(tenant_a.catalog_version_id), "SKU-1001")
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"description": "bolts", "quantity": "10", "product_id": bolt_v1["id"]}],
        },
    ).json()
    _import(admin, "v2.csv", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90,0.52\n", activate=True)
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "notes": "quantity corrected",
            "lines": [_editor_payload(ln, quantity="12")],
        },
    )
    assert r.status_code == 200, r.text
    ln2 = r.json()["current_version"]["lines"][0]
    assert ln2["quantity"] == "12" and ln2["unit_price"] == "0.85" and ln2["product_id"] == bolt_v1["id"]


def test_n2_1b_rfq_pinned_quote_prices_only_from_its_own_version_and_new_versions_keep_the_pin(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """Documents the pin: a quote generated from an RFQ matched against v1 keeps v1 as its price authority after
    v2 is activated (v1 products re-linkable at v1 prices; v2 products refused), and a later new version inherits
    the pin. The security invariant holds (no version that was never active for this quote is ever a price
    authority); see observation O2-1 on the UX of the refusal message."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    v1 = str(tenant_a.catalog_version_id)
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    assert q["current_version"]["pricing_snapshot"]["catalog_version_id"] == v1
    v2 = _import(
        admin,
        "v2.csv",
        b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90,0.52\nSKU-1003,Hex nut M8 zinc plated,EA,AED,0.30,0.14\n",
        activate=True,
    )
    ln = q["current_version"]["lines"][0]
    nut_v2 = _product(sales, v2["id"], "SKU-1003")
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [_editor_payload(ln, product_id=nut_v2["id"])]},
    )
    assert r.status_code == 422, r.text  # the active version is not this quote's price authority
    nut_v1 = _product(sales, v1, "SKU-1003")
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [_editor_payload(ln, product_id=nut_v1["id"], unit_price=None)],
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["current_version"]["lines"][0]["unit_price"] == "0.25"  # v1 price, not v2's 0.30
    _, q = _approve(app, tenant_a, sales, q)
    q = _new_version(sales, q)
    assert q["current_version"]["pricing_snapshot"]["catalog_version_id"] == v1
    ln = q["current_version"]["lines"][0]
    assert ln["unit_price"] == "0.25" and ln["price_source"] == "catalog"


# ---------------------------------------------------------------------------------------------------------------
# N2-2 — withdrawal sticks to every approved version (S1-F15, S1-A15, S1-A18)
# ---------------------------------------------------------------------------------------------------------------


def test_n2_2_two_approved_versions_stay_withdrawn_for_every_role_through_v3_draft_and_v3_approval(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _approve(app, tenant_a, sales, q)
    q = _new_version(sales, q, notes="v2 notes")
    approver, q = _approve(app, tenant_a, sales, q)
    sha = {v["version_no"]: v["pdf_sha256"] for v in q["versions"]}
    assert sha[1] and sha[2] and sha[1] != sha[2]
    for n in (1, 2):
        _assert_exportable_for_every_role(app, tenant_a, q["id"], n)

    w = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "tender lost"}
    )
    assert w.status_code == 200, w.text
    q = w.json()
    assert q["status"] == "withdrawn" and q["effective_status"] == "withdrawn"
    assert [v["status"] for v in q["versions"]] == ["withdrawn", "withdrawn"]
    for v in q["versions"]:
        assert [d["decision"] for d in v["decisions"]] == ["approved", "withdrawn"], v["version_no"]
        assert v["decisions"][-1]["reason"] == "tender lost"
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1, 2])
    lv = q["lock_version"]
    assert approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv}).status_code == 409
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": lv}).status_code == 409
    hist = sales.get(f"/quotes/{q['id']}/history").json()
    wd = [e for e in hist["audit"] if e["action"] == "quote.withdraw"]
    assert len(wd) == 1 and sorted(wd[0]["summary"]["withdrawn_versions"]) == [1, 2]

    # v3 draft: the old documents stay blocked; the draft preview is clearly not an approved document
    q = _new_version(sales, q, notes="v3 after withdrawal")
    assert q["status"] == "draft" and q["current_version_no"] == 3 and q["withdrawn_reason"] is None
    assert [v["status"] for v in q["versions"]] == ["withdrawn", "withdrawn", "draft"]
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1, 2])
    preview = sales.get(f"/quotes/{q['id']}/versions/3/pdf")
    assert preview.status_code == 200 and "NOT APPROVED" in _pdf_text(preview.content)
    # v3 approved: exportable for everyone, v1/v2 still blocked, stored hashes of the withdrawn documents untouched
    approver, q = _approve(app, tenant_a, sales, q)
    assert q["status"] == "approved"
    pdf3 = _assert_exportable_for_every_role(app, tenant_a, q["id"], 3)
    assert "APPROVED" in _pdf_text(pdf3)
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1, 2])
    assert {v["version_no"]: v["pdf_sha256"] for v in q["versions"] if v["version_no"] < 3} == sha
    # withdrawing again (v3) adds exactly one decision to v3 and none to v1/v2
    owner = login_as(app, tenant_a.users["owner"])
    w = owner.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "again"})
    assert w.status_code == 200, w.text
    q = w.json()
    assert [v["status"] for v in q["versions"]] == ["withdrawn", "withdrawn", "withdrawn"]
    assert [[d["decision"] for d in v["decisions"]] for v in q["versions"]] == [["approved", "withdrawn"]] * 3
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1, 2, 3])


def test_n2_2_approver_withdraws_v1_while_v2_draft_is_open_then_approved_v2_exports_while_v1_stays_blocked(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _approve(app, tenant_a, sales, q)
    q = _new_version(sales, q)
    assert q["status"] == "draft"
    _assert_exportable_for_every_role(app, tenant_a, q["id"], 1)
    w = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "v1 priced wrong"}
    )
    assert w.status_code == 200, w.text
    q = w.json()
    assert q["status"] == "draft" and q["effective_status"] == "draft" and q["withdrawn_reason"] == "v1 priced wrong"
    assert [v["status"] for v in q["versions"]] == ["withdrawn", "draft"]
    assert [d["decision"] for d in q["versions"][0]["decisions"]] == ["approved", "withdrawn"]
    assert q["versions"][1]["decisions"] == []
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1])
    # the draft is still editable and previewable, and there is nothing left to withdraw
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "quantity": "7"}]},
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["current_version"]["lines"][0]["quantity"] == "7" and q["blockers"] == []
    preview = sales.get(f"/quotes/{q['id']}/versions/2/pdf")
    assert preview.status_code == 200 and "NOT APPROVED" in _pdf_text(preview.content)
    assert (
        approver.post(
            f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "twice"}
        ).status_code
        == 409
    )
    # v2 goes through approval: the new document exports, the withdrawn one never does
    approver, q = _approve(app, tenant_a, sales, q)
    assert q["status"] == "approved" and q["effective_status"] == "approved"
    assert [v["status"] for v in q["versions"]] == ["withdrawn", "approved"]
    pdf2 = _assert_exportable_for_every_role(app, tenant_a, q["id"], 2)
    assert "APPROVED" in _pdf_text(pdf2) and "WITHDRAWN" not in _pdf_text(pdf2)
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1])
    approved_payload = q
    # a further version from the approved v2 leaves v2 exportable and v1 blocked
    q = _new_version(sales, q)
    assert _export_status(sales, q["id"], 2) == (200, 200, 200)
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1])
    # an approved quote must not still describe itself as withdrawn (the withdrawal concerned v1 only; the stale
    # reason is what migration 0005's data fix keys on and what the UI shows as "Withdrawn")
    assert approved_payload["withdrawn_reason"] is None, approved_payload["withdrawn_reason"]


def test_n2_2_withdraw_while_v2_is_pending_supersedes_v2_withdraws_v1_and_v3_can_still_be_approved(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _approve(app, tenant_a, sales, q)
    q = _new_version(sales, q)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    q = s.json()
    w = owner.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "stop"})
    assert w.status_code == 200, w.text
    q = w.json()
    assert q["status"] == "withdrawn"
    assert [v["status"] for v in q["versions"]] == ["withdrawn", "superseded"]
    assert [d["decision"] for d in q["versions"][1]["decisions"]] == ["withdrawn"]
    lv = q["lock_version"]
    assert approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv}).status_code == 409
    assert (
        approver.post(f"/quotes/{q['id']}/reject", json={"expected_lock_version": lv, "reason": "x"}).status_code == 409
    )
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1])
    # the superseded pending version is not an approved document either
    for role in ROLES:
        c = login_as(app, tenant_a.users[role])
        p = c.get(f"/quotes/{q['id']}/versions/2/pdf")
        assert p.status_code != 200 or "APPROVED" not in _pdf_text(p.content).replace("NOT APPROVED", ""), role
    q = _new_version(sales, q)
    assert q["status"] == "draft" and q["current_version_no"] == 3
    approver, q = _approve(app, tenant_a, sales, q)
    _assert_exportable_for_every_role(app, tenant_a, q["id"], 3)
    _assert_blocked_for_every_role(app, tenant_a, q["id"], [1])


def test_n2_2_withdraw_while_draft_role_lock_and_reason_matrix_leaves_v1_exportable_and_the_draft_untouched(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    readonly = login_as(app, tenant_a.users["readonly"])
    admin = login_as(app, tenant_a.users["admin"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _approve(app, tenant_a, sales, q)
    q = _new_version(sales, q)
    lv = q["lock_version"]
    attempts = [
        (sales, {"expected_lock_version": lv, "reason": "sales may not"}, 403),
        (readonly, {"expected_lock_version": lv, "reason": "readonly may not"}, 403),
        (approver, {"expected_lock_version": lv - 1, "reason": "stale lock"}, 409),
        (approver, {"expected_lock_version": lv, "reason": "   "}, 422),
        (approver, {"expected_lock_version": lv}, 422),
    ]
    for client, body, expected in attempts:
        r = client.post(f"/quotes/{q['id']}/withdraw", json=body)
        assert r.status_code == expected, (body, r.status_code, r.text)
        now = sales.get(f"/quotes/{q['id']}").json()
        assert now["lock_version"] == lv and now["status"] == "draft" and now["withdrawn_reason"] is None
        assert [v["status"] for v in now["versions"]] == ["approved", "draft"]
        assert _export_status(sales, q["id"], 1) == (200, 200, 200)
    # admin (not only approver/owner) may withdraw while the draft is open
    r = admin.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": lv, "reason": "admin withdraws"})
    assert r.status_code == 200, r.text
    assert [v["status"] for v in r.json()["versions"]] == ["withdrawn", "draft"]
    # a quote whose only approval attempt was rejected has nothing to withdraw
    q2 = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    s = sales.post(f"/quotes/{q2['id']}/submit", json={"expected_lock_version": q2["lock_version"]}).json()
    rj = approver.post(
        f"/quotes/{q2['id']}/reject", json={"expected_lock_version": s["lock_version"], "reason": "too cheap"}
    )
    assert rj.status_code == 200 and rj.json()["status"] == "draft"
    assert (
        approver.post(
            f"/quotes/{q2['id']}/withdraw", json={"expected_lock_version": rj.json()["lock_version"], "reason": "x"}
        ).status_code
        == 409
    )


def _owner_sql(org_id: uuid.UUID, sql: str, params: dict[str, Any]) -> Any:
    """Run a statement as the owner role inside the tenant (every tenant table has FORCE ROW LEVEL SECURITY, so
    even the owner sees nothing without `app.current_org`)."""
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True)
    try:
        with eng.begin() as conn:
            conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org_id)})
            res = conn.execute(text(sql), params)
            return res.scalar() if sql.lstrip().upper().startswith("SELECT") else res.rowcount  # type: ignore[attr-defined]
    finally:
        eng.dispose()


def _replay_migration_0005() -> None:
    env = {**os.environ}
    for args in (["downgrade", "-1"], ["upgrade", "head"]):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=API_DIR, env=env, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr[-2000:]


def test_l2_5_migration_0005_data_fix_withdraws_an_approved_version_left_under_a_withdrawn_quote(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """Replays the data fix on a real row of the exact shape it targets (quote withdrawn, `withdrawn_reason` set,
    version still `approved` — the pre-round-4 shape). After 0005 is re-applied the version must be `withdrawn`
    and the identity triggers must be back."""
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    approver, q = _approve(app, tenant_a, sales, q)
    w = approver.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "r"})
    assert w.status_code == 200 and w.json()["versions"][0]["status"] == "withdrawn"
    assert (
        _owner_sql(
            tenant_a.org_id,
            "UPDATE quote_versions SET status = 'approved' WHERE quote_id = :q AND version_no = 1",
            {"q": q["id"]},
        )
        == 1
    )
    assert sales.get(f"/quotes/{q['id']}").json()["versions"][0]["status"] == "approved"  # legacy shape in place
    _replay_migration_0005()
    status = _owner_sql(
        tenant_a.org_id, "SELECT status FROM quote_versions WHERE quote_id = :q AND version_no = 1", {"q": q["id"]}
    )
    assert status == "withdrawn", f"0005 data fix left the legacy approved version as {status!r}"
    assert (
        _owner_sql(
            tenant_a.org_id,
            "SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_sessions_identity','trg_users_identity')",
            {},
        )
        == 2
    )
    assert sales.get(f"/quotes/{q['id']}").json()["versions"][0]["status"] == "withdrawn"
    assert _export_status(sales, q["id"], 1) == (409, 409, 409)


def test_l2_5_migration_0005_data_fix_also_covers_a_withdrawn_document_whose_quote_already_has_a_new_version(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """The legacy shape the reviewer actually flagged (R-1/N2-2): withdrawn, then a replacement draft was opened,
    which cleared `withdrawn_reason`; the version row still says `approved` while its decision trail says
    `withdrawn`. Such a document is exportable again as APPROVED unless the data fix reaches it."""
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    approver, q = _approve(app, tenant_a, sales, q)
    w = approver.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "r"})
    assert w.status_code == 200
    q = _new_version(sales, w.json())
    assert q["status"] == "draft" and q["withdrawn_reason"] is None
    assert (
        _owner_sql(
            tenant_a.org_id,
            "UPDATE quote_versions SET status = 'approved' WHERE quote_id = :q AND version_no = 1",
            {"q": q["id"]},
        )
        == 1
    )
    assert _export_status(sales, q["id"], 1) == (200, 200, 200)  # the revived legacy document, before the fix
    assert [d["decision"] for d in sales.get(f"/quotes/{q['id']}").json()["versions"][0]["decisions"]] == [
        "approved",
        "withdrawn",
    ]
    _replay_migration_0005()
    status = _owner_sql(
        tenant_a.org_id, "SELECT status FROM quote_versions WHERE quote_id = :q AND version_no = 1", {"q": q["id"]}
    )
    assert status == "withdrawn", f"0005 data fix left the revived legacy document as {status!r}"
    assert _export_status(sales, q["id"], 1) == (409, 409, 409)


# ---------------------------------------------------------------------------------------------------------------
# R-2 / R-3 / R-4 re-checks (predecessor defects fixed in round 3)
# ---------------------------------------------------------------------------------------------------------------


def test_r2_margin_floor_is_absent_from_every_quote_payload_for_sales_and_readonly_but_visible_to_privileged(
    app,
) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Floor Probe", min_margin="37.5")
    sales = login_as(app, t.users["sales"])
    q = make_quote(sales, str(t.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    blocked = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [_editor_payload(ln, discount_pct="40")]},
    )
    assert blocked.status_code == 200 and "below_min_margin" in _codes(blocked.json())
    submit = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": blocked.json()["lock_version"]})
    assert submit.status_code == 409
    for role in ("sales", "readonly"):
        c = login_as(app, t.users[role])
        payloads = [
            c.get(f"/quotes/{q['id']}").json(),
            c.get(f"/quotes/{q['id']}/history").json(),
            c.get("/quotes").json(),
            c.get("/org").json(),
            blocked.json() if role == "sales" else {},
            submit.json() if role == "sales" else {},
        ]
        for p in payloads:
            assert _walk(p, "min_margin_pct") == [], (role, p)
            assert _walk(p, "cost_price") == [] and _walk(p, "margin_pct") == [], role
            assert "37.5" not in json.dumps(p), (role, p)
    for role in ("owner", "admin", "approver"):
        c = login_as(app, t.users[role])
        snaps = _walk(c.get(f"/quotes/{q['id']}").json(), "pricing_snapshot")
        assert snaps and all(Decimal(s.get("min_margin_pct")) == Decimal("37.5") for s in snaps), role


def test_r3_r4_editor_style_remove_first_reorder_and_replace_middle_line_in_single_saves(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    lines = q["current_version"]["lines"]
    assert len(lines) >= 3
    skus = [ln["sku"] for ln in lines]
    # reverse the order in one save (full editor payload)
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [_editor_payload(ln) for ln in reversed(lines)]},
    )
    assert r.status_code == 200, r.text
    q = r.json()
    got = q["current_version"]["lines"]
    assert [ln["line_no"] for ln in got] == list(range(1, len(lines) + 1))
    assert [ln["sku"] for ln in got] == list(reversed(skus))
    assert q["current_version"]["grand_total"] == r.json()["current_version"]["grand_total"]
    # remove the first line and replace the (new) middle line with a custom one, in one save
    lines = got
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                _editor_payload(lines[-1]),
                {"description": "Replacement bracket", "quantity": "2", "unit_price": "4", "is_custom": True},
            ],
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    got = q["current_version"]["lines"]
    assert [ln["line_no"] for ln in got] == [1, 2]
    assert got[0]["sku"] == skus[0] and got[1]["description"] == "Replacement bracket" and got[1]["is_custom"]
    # and the editor payload shape after that save still round-trips without a server error
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [_editor_payload(ln) for ln in got]},
    )
    assert r.status_code == 200, r.text
    assert str(uuid.UUID(got[1]["id"])) == got[1]["id"]


def test_withdraw_cannot_be_used_to_leak_or_alter_lines_of_another_tenant(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """Cross-tenant: the widened withdraw path (every approved version, draft current) must still be 404 for a
    foreign approver, and leave the quote exportable."""
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    approver, q = _approve(app, tenant_a, sales, q)
    q = _new_version(sales, q)
    foreign = login_as(app, tenant_b.users["approver"])
    r = foreign.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "x"})
    assert r.status_code == 404, r.text
    assert _export_status(sales, q["id"], 1) == (200, 200, 200)
    assert FIXTURES.exists()
