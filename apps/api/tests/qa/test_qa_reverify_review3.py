"""QA (independent) re-verification 3 of Stage 1 at f69ac25 (fix rounds 5 and 6).

Derived from docs/evidence/release-review-3-stage1-2026-11-02.md ("Required retest": D2-1..D2-5, R3-6, R3-10, explicit
FORCE-RLS check after the replay), docs/PRD.md (S1-F11, S1-F12, S1-F13, S1-F14, S1-F15, X-01) and
docs/API_CONTRACT.md. Probes differ from tests/integration/test_review2_fixes.py and the review-2 QA module:
price formatting variants, manual-price laundering attempts, mixed-order editor payloads, multi-tenant replays of the
withdrawal data fix, every role on the new `GET /catalogs/active-version`, and the stale-line lifecycle through a
pending approval. Integration evidence (API + PostgreSQL + RLS through the ASGI TestClient), not browser evidence.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import Tenant, login_as, make_tenant
from tests.integration.test_quotes import make_quote
from tests.qa.test_qa_reverify_review2 import (
    ROLES,
    _approve,
    _codes,
    _editor_payload,
    _export_status,
    _import,
    _new_version,
    _owner_sql,
    _product,
    _replay_migration_0005,
)

pytestmark = pytest.mark.integration

BOLT_TEXT = "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n"
V2_ROWS = b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.91,0.52\n"


def _adhoc_linked(sales, tenant: Tenant, sku: str = "SKU-1001", qty: str = "3"):  # type: ignore[no-untyped-def]
    old = sales.get(f"/catalogs/versions/{tenant.catalog_version_id}/products?q={sku}").json()["items"][0]
    q = sales.post("/quotes", json={"customer_id": str(tenant.customer_id)}).json()
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"quantity": qty, "product_id": old["id"]}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_version"]["lines"][0]["price_source"] == "catalog"
    return r.json(), old


# ---------------------------------------------------------------------------------------------------------------
# D2-1 / NEW-17: provenance of re-sent prices
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("resent", ["0.85", "0.850000", "0.8500", "0.85000000"])
def test_d2_1_price_formatting_variants_of_the_catalog_price_keep_catalog_provenance(
    app, tenant_a: Tenant, resent: str
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), BOLT_TEXT)
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [_editor_payload(ln, unit_price=resent)]},
    )
    assert r.status_code == 200, r.text
    got = r.json()["current_version"]["lines"][0]
    assert got["price_source"] == "catalog", (resent, got["price_source"])
    assert r.json()["blockers"] == []


def test_d2_1_price_key_omitted_discount_and_quantity_edits_keep_catalog_and_totals_are_exact(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """The round-5 editor omits `unit_price` for untouched catalog lines; a discount + quantity edit must keep
    `catalog` and be priced on the Decimal path (500 x 0.85, 7.5 % discount, 5 % tax: S1-F11 rounding)."""
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), BOLT_TEXT)
    ln = q["current_version"]["lines"][0]
    payload = _editor_payload(ln, quantity="500", discount_pct="7.5")
    del payload["unit_price"]
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [payload]})
    assert r.status_code == 200, r.text
    got = r.json()["current_version"]["lines"][0]
    assert got["price_source"] == "catalog" and got["unit_price"] == ln["unit_price"]
    assert (got["discount_amount"], got["tax_amount"], got["line_total"]) == ("31.88", "19.66", "412.78")
    assert r.json()["blockers"] == []


def test_d2_1_a_changed_price_is_manual_stays_manual_on_resend_and_cannot_be_laundered_back_to_catalog(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    q = make_quote(sales, str(tenant_a.customer_id), BOLT_TEXT)
    ln = q["current_version"]["lines"][0]

    def save(price: str | None, q: dict[str, Any]) -> dict[str, Any]:
        cur = q["current_version"]["lines"][0]
        p = _editor_payload(cur)
        p["unit_price"] = price
        r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [p]})
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    q = save("0.86", q)
    assert q["current_version"]["lines"][0]["price_source"] == "manual" and "manual_price_unconfirmed" in _codes(q)
    q = save("0.86", q)  # editor re-send of the manual price
    assert q["current_version"]["lines"][0]["price_source"] == "manual"
    # typing the catalog price back into a manual line must NOT silently restore `catalog`
    q = save("0.85", q)
    assert q["current_version"]["lines"][0]["price_source"] == "manual"
    assert "manual_price_unconfirmed" in _codes(q)
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409
    # an explicit null on a linked line restores the catalog price and provenance
    q = save(None, q)
    got = q["current_version"]["lines"][0]
    assert (got["price_source"], got["unit_price"]) == ("catalog", ln["unit_price"]) and q["blockers"] == []
    # a confirmed manual price is voided when the price changes again
    q = save("0.70", q)
    lid = q["current_version"]["lines"][0]["id"]
    ok = owner.post(
        f"/quotes/{q['id']}/lines/{lid}/price-override",
        json={"expected_lock_version": q["lock_version"], "reason": "agreed with customer"},
    )
    assert ok.status_code == 200, ok.text
    q = ok.json()
    assert not q["blockers"] or "manual_price_unconfirmed" not in _codes(q)
    q = save("0.71", q)
    assert "manual_price_unconfirmed" in _codes(q), "changing a confirmed price must require a new confirmation"


def test_d2_1_custom_line_with_the_catalog_price_typed_in_is_manual_not_catalog(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), BOLT_TEXT)
    ln = q["current_version"]["lines"][0]
    p = _editor_payload(ln, is_custom=True, product_id=None, unit_price="0.85")
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [p]})
    assert r.status_code == 200, r.text
    got = r.json()["current_version"]["lines"][0]
    assert (got["price_source"], got["is_custom"], got["product_id"]) == ("manual", True, None)
    assert "manual_price_unconfirmed" in _codes(r.json())


# ---------------------------------------------------------------------------------------------------------------
# D2-2 / NEW-13 / R3-6: ad-hoc quote across a catalog activation
# ---------------------------------------------------------------------------------------------------------------


def test_d2_2_unpinned_draft_saves_after_activation_with_either_editor_shape_and_reports_a_blocker(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    q, old = _adhoc_linked(sales, tenant_a)
    _import(admin, "v2.csv", V2_ROWS, activate=True)
    # old-style full re-send (product_id + unit_price) and new-style (price omitted): both must save
    full = _editor_payload(q["current_version"]["lines"][0], quantity="12")
    r = sales.patch(
        f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "notes": "n1", "lines": [full]}
    )
    assert r.status_code == 200, r.text
    q = r.json()
    got = q["current_version"]["lines"][0]
    assert got["quantity"] == "12" and got["product_id"] == old["id"] and got["unit_price"] == "0.85"
    assert got["price_source"] == "catalog"
    assert [b["code"] for b in q["blockers"]] == ["catalog_version_mismatch"]
    slim = dict(full, quantity="13")
    del slim["unit_price"]
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [slim]})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["current_version"]["lines"][0]["quantity"] == "13"
    assert "catalog_version_mismatch" in _codes(q)
    # an untouched stale link can never be submitted at the archived price
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 409 and "catalog_version_mismatch" in s.text


def test_r3_6_relink_to_the_active_catalog_reprices_server_side_and_clears_the_blocker(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    q, _old = _adhoc_linked(sales, tenant_a)
    v2 = _import(admin, "v2.csv", V2_ROWS, activate=True)
    av = sales.get("/catalogs/active-version")
    assert av.status_code == 200 and av.json() == {"version_id": v2["id"]}
    new = _product(sales, v2["id"], "SKU-1001")
    # the browser's shape after "Use active catalog": new product_id, price key omitted
    p = _editor_payload(q["current_version"]["lines"][0], product_id=new["id"])
    del p["unit_price"]
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [p]})
    assert r.status_code == 200, r.text
    got = r.json()["current_version"]["lines"][0]
    assert (got["product_id"], got["unit_price"], got["price_source"]) == (new["id"], "0.91", "catalog")
    assert r.json()["blockers"] == []
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": r.json()["lock_version"]})
    assert s.status_code == 200, s.text


def test_r3_6_relink_that_also_resends_the_stale_price_never_yields_a_wrong_catalog_price(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    """A client that re-links to the active product but still sends the old 0.85 must not end as a `catalog` line at
    0.85 (the archived price): it is either re-priced at 0.91 `catalog` or becomes `manual` and blocks."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    q, _old = _adhoc_linked(sales, tenant_a)
    v2 = _import(admin, "v2.csv", V2_ROWS, activate=True)
    new = _product(sales, v2["id"], "SKU-1001")
    p = _editor_payload(q["current_version"]["lines"][0], product_id=new["id"])  # unit_price "0.85" kept
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [p]})
    assert r.status_code == 200, r.text
    got = r.json()["current_version"]["lines"][0]
    assert not (got["price_source"] == "catalog" and got["unit_price"] == "0.85"), got
    if got["price_source"] == "manual":
        assert "manual_price_unconfirmed" in _codes(r.json())


def test_r3_6_stale_line_cannot_be_relinked_to_archived_or_foreign_products_and_custom_switch_needs_confirmation(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    old = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001").json()["items"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"quantity": "3", "product_id": old["id"]}]},
    ).json()
    _import(admin, "v2.csv", V2_ROWS, activate=True)
    ln = q["current_version"]["lines"][0]
    # a *new* line linked to an archived-version product is refused (422) and nothing changes
    bad = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"id": ln["id"]}, {"quantity": "1", "product_id": old["id"]}],
        },
    )
    assert bad.status_code == 422, bad.text
    # foreign tenant product: 404, not an existence oracle distinct from a missing one
    b_prod = (
        login_as(app, tenant_b.users["sales"])
        .get(f"/catalogs/versions/{tenant_b.catalog_version_id}/products?q=SKU-1001")
        .json()["items"][0]
    )
    foreign = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "product_id": b_prod["id"]}]},
    )
    assert foreign.status_code == 404, foreign.text
    # converting the stale line to custom: the mismatch blocker is replaced by the manual-price confirmation
    cu = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [_editor_payload(ln, is_custom=True, product_id=None, unit_price="0.85")],
        },
    )
    assert cu.status_code == 200, cu.text
    assert _codes(cu.json()) == {"manual_price_unconfirmed"}
    # lock was not advanced by the refused saves (atomic)
    assert bad.status_code == 422 and q["lock_version"] < cu.json()["lock_version"]


def test_r3_10_pending_stale_quote_cannot_be_approved_by_any_approver_role_and_recovers_via_reject_new_version_relink(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    q, _old = _adhoc_linked(sales, tenant_a)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    pend = s.json()
    v2 = _import(admin, "v2.csv", V2_ROWS, activate=True)
    for role in ("approver", "owner", "admin"):
        c = login_as(app, tenant_a.users[role])
        r = c.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": pend["lock_version"]})
        assert r.status_code == 409 and "catalog_version_mismatch" in r.text, (role, r.status_code, r.text)
        again = c.get(f"/quotes/{q['id']}").json()
        assert again["status"] == "pending_approval" and again["current_version"]["status"] == "pending_approval"
    approver = login_as(app, tenant_a.users["approver"])
    rj = approver.post(
        f"/quotes/{q['id']}/reject", json={"expected_lock_version": pend["lock_version"], "reason": "catalog moved"}
    )
    assert rj.status_code == 200 and rj.json()["status"] == "draft"
    nv = sales.get(f"/quotes/{q['id']}").json()  # rejected -> the same version is a draft again
    assert "catalog_version_mismatch" in _codes(nv)  # the stale line is still blocked after the rejection
    new = _product(sales, v2["id"], "SKU-1001")
    ln = nv["current_version"]["lines"][0]
    p = _editor_payload(ln, product_id=new["id"])
    del p["unit_price"]
    fixed = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": nv["lock_version"], "lines": [p]})
    assert fixed.status_code == 200 and fixed.json()["blockers"] == [], fixed.text
    _ap, done = _approve(app, tenant_a, sales, fixed.json())
    assert done["status"] == "approved"
    assert done["current_version"]["lines"][0]["unit_price"] == "0.91"
    for role in ROLES:
        c = login_as(app, tenant_a.users[role])
        assert _export_status(c, q["id"], 1) == (200, 200, 200), role


# ---------------------------------------------------------------------------------------------------------------
# GET /catalogs/active-version (new in round 6)
# ---------------------------------------------------------------------------------------------------------------


def test_active_version_endpoint_requires_auth_is_tenant_scoped_and_null_without_a_catalog(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    anon = TestClient(app, base_url="http://testserver")
    assert anon.get("/api/v1/catalogs/active-version").status_code in (401, 403)
    ids = {}
    for name, t in (("a", tenant_a), ("b", tenant_b)):
        for role in ROLES:
            r = login_as(app, t.users[role]).get("/catalogs/active-version")
            assert r.status_code == 200, (name, role, r.text)
            assert set(r.json()) == {"version_id"}
            ids.setdefault(name, set()).add(r.json()["version_id"])
    assert ids["a"] == {str(tenant_a.catalog_version_id)} and ids["b"] == {str(tenant_b.catalog_version_id)}
    assert ids["a"].isdisjoint(ids["b"])
    empty = make_tenant("No Catalog Org", with_catalog=False)
    assert login_as(app, empty.users["sales"]).get("/catalogs/active-version").json() == {"version_id": None}
    # importing without activating does not move the pointer
    admin = login_as(app, tenant_a.users["admin"])
    _import(admin, "pending.csv", V2_ROWS, activate=False)
    assert admin.get("/catalogs/active-version").json() == {"version_id": str(tenant_a.catalog_version_id)}
    # tenant A's activation is invisible to tenant B
    _import(admin, "v3.csv", V2_ROWS, activate=True)
    assert admin.get("/catalogs/active-version").json()["version_id"] != str(tenant_a.catalog_version_id)
    assert login_as(app, tenant_b.users["admin"]).get("/catalogs/active-version").json() == {
        "version_id": str(tenant_b.catalog_version_id)
    }


# ---------------------------------------------------------------------------------------------------------------
# D2-5: withdrawn_reason hygiene
# ---------------------------------------------------------------------------------------------------------------


def test_d2_5_reason_is_cleared_when_a_later_version_is_approved_and_set_again_by_a_later_withdrawal(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), BOLT_TEXT)
    approver, q = _approve(app, tenant_a, sales, q)
    q = _new_version(sales, q)
    w = approver.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "r1"})
    assert w.status_code == 200 and w.json()["withdrawn_reason"] == "r1" and w.json()["status"] == "draft"
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": w.json()["lock_version"]})
    assert s.status_code == 200
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert a.status_code == 200 and a.json()["status"] == "approved" and a.json()["withdrawn_reason"] is None
    assert [v["status"] for v in a.json()["versions"]] == ["withdrawn", "approved"]
    h = approver.get(f"/quotes/{q['id']}").json()
    assert h["withdrawn_reason"] is None
    # v1 stays blocked, v2 live for every role
    for role in ROLES:
        c = login_as(app, tenant_a.users[role])
        assert _export_status(c, q["id"], 1) == (409, 409, 409)
        assert _export_status(c, q["id"], 2) == (200, 200, 200)
    # a later withdrawal of v2 sets a fresh reason and withdraws the quote
    w2 = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": a.json()["lock_version"], "reason": "r2"}
    )
    assert w2.status_code == 200 and w2.json()["status"] == "withdrawn" and w2.json()["withdrawn_reason"] == "r2"
    assert [v["status"] for v in w2.json()["versions"]] == ["withdrawn", "withdrawn"]
    # re-approving a withdrawn quote through a new version again clears the reason
    nv = _new_version(sales, w2.json())
    assert nv["withdrawn_reason"] is None
    _ap, done = _approve(app, tenant_a, sales, nv)
    assert done["withdrawn_reason"] is None and done["status"] == "approved"


def test_d2_5_withdraw_while_pending_then_approving_a_replacement_leaves_no_reason(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), BOLT_TEXT)
    approver, q = _approve(app, tenant_a, sales, q)
    q = _new_version(sales, q)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    w = approver.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": s["lock_version"], "reason": "rp"})
    assert w.status_code == 200, w.text
    assert [v["status"] for v in w.json()["versions"]] == ["withdrawn", "superseded"]
    assert w.json()["status"] == "withdrawn"
    nv = _new_version(sales, w.json())
    _ap, done = _approve(app, tenant_a, sales, nv)
    assert done["withdrawn_reason"] is None
    assert [v["status"] for v in done["versions"]] == ["withdrawn", "superseded", "approved"]


# ---------------------------------------------------------------------------------------------------------------
# D2-3 / R3-10: migration data fix, multi-tenant, idempotent, FORCE RLS restored
# ---------------------------------------------------------------------------------------------------------------


def _force_rls_state() -> dict[str, tuple[bool, bool]]:
    import os

    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True)
    try:
        with eng.connect() as c:
            tables = [
                r[0]
                for r in c.execute(
                    text(
                        "SELECT DISTINCT table_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND column_name='organization_id'"
                    )
                )
            ]
            rows = c.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relnamespace='public'::regnamespace AND relname = ANY(:t)"
                ),
                {"t": tables},
            ).all()
        return {r[0]: (r[1], r[2]) for r in rows}
    finally:
        eng.dispose()


def test_r3_10_force_rls_is_on_for_every_tenant_table_after_the_replay_and_policies_still_isolate(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    _replay_migration_0005()
    state = _force_rls_state()
    assert {"quotes", "quote_versions", "quote_lines", "approval_decisions"} <= set(state)
    # `sessions` is the documented exception (resolved by token hash before any tenant context): ENABLE only
    assert state["sessions"][0] is True
    off = {t: s for t, s in state.items() if s != (True, True) and t != "sessions"}
    assert not off, f"tables without ENABLE+FORCE RLS after replay: {off}"
    # the owner without a tenant context sees nothing (FORCE is effective), with one sees only that tenant
    import os

    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True)
    try:
        with eng.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM customers")).scalar() == 0
            conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(tenant_a.org_id)})
            assert conn.execute(text("SELECT count(DISTINCT organization_id) FROM customers")).scalar() == 1
    finally:
        eng.dispose()


def test_d2_3_replay_fixes_every_tenant_is_idempotent_and_does_not_touch_legitimate_approvals(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    withdrawn_ids: list[tuple[Tenant, str]] = []
    live_ids: list[tuple[Tenant, str]] = []
    for t in (tenant_a, tenant_b):
        sales = login_as(app, t.users["sales"])
        # q1: legacy shape (withdrawn, row reverted to approved)
        q1 = make_quote(sales, str(t.customer_id), BOLT_TEXT)
        ap, q1 = _approve(app, t, sales, q1)
        w = ap.post(f"/quotes/{q1['id']}/withdraw", json={"expected_lock_version": q1["lock_version"], "reason": "r"})
        assert w.status_code == 200
        # q2: v1 withdrawn, v2 legitimately approved later (higher version_no, later decision)
        q2 = make_quote(sales, str(t.customer_id), BOLT_TEXT)
        ap2, q2 = _approve(app, t, sales, q2)
        q2 = _new_version(sales, q2)
        w2 = ap2.post(f"/quotes/{q2['id']}/withdraw", json={"expected_lock_version": q2["lock_version"], "reason": "r"})
        assert w2.status_code == 200
        _ap, q2 = _approve(app, t, sales, w2.json())
        # q3: plain approved, never withdrawn
        q3 = make_quote(sales, str(t.customer_id), BOLT_TEXT)
        _ap3, q3 = _approve(app, t, sales, q3)
        assert (
            _owner_sql(
                t.org_id,
                "UPDATE quote_versions SET status='approved' WHERE quote_id=:q AND version_no=1",
                {"q": q1["id"]},
            )
            == 1
        )
        assert (
            _owner_sql(
                t.org_id,
                "UPDATE quote_versions SET status='approved' WHERE quote_id=:q AND version_no=1",
                {"q": q2["id"]},
            )
            == 1
        )
        withdrawn_ids += [(t, q1["id"]), (t, q2["id"])]
        live_ids += [(t, q2["id"]), (t, q3["id"])]
    for _ in range(2):  # second replay must be a no-op (idempotent)
        _replay_migration_0005()
        for t, qid in withdrawn_ids:
            st = _owner_sql(
                t.org_id, "SELECT status FROM quote_versions WHERE quote_id=:q AND version_no=1", {"q": qid}
            )
            assert st == "withdrawn", (t.name, qid, st)
        # the legitimately approved latest versions are untouched
        for t, qid in live_ids:
            n = _owner_sql(
                t.org_id,
                "SELECT count(*) FROM quote_versions WHERE quote_id=:q AND status='approved'",
                {"q": qid},
            )
            assert n == 1, (t.name, qid, n)
        assert all(s == (True, True) for t, s in _force_rls_state().items() if t != "sessions")
    # exports follow the repaired state
    for t, qid in withdrawn_ids:
        assert _export_status(login_as(app, t.users["sales"]), qid, 1) == (409, 409, 409)
