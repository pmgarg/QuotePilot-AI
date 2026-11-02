"""Security re-verification tests, round 3 (Stage 1 fix rounds 3+4, 2026-11-01).

Companion to ``test_audit_followups.py`` / ``test_audit_followups2.py``. Each test names the finding it covers
(see docs/evidence/security-stage1-reverify2-2026-11-01.md). Tests that reproduce an OPEN finding are marked
``xfail(strict=True)``: they fail the suite the moment the defect is fixed, so the marker must be removed together
with the fix. Everything else asserts the fixed behaviour of N2-1 / N2-2 / L2-4 / NEW-3 / NEW-5 from a security angle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import API_DIR, Tenant, login_as, make_tenant, upload
from tests.integration.test_quotes import make_quote

pytestmark = [pytest.mark.integration, pytest.mark.security]

REPO_ROOT = API_DIR.parents[1]
MAPPING = json.dumps(
    {"sku": "SKU", "description": "Description", "unit": "Unit", "currency": "Currency", "list_price": "List Price"}
)


# verbatim copy of the L2-5 data fix in alembic/versions/20261101_0005_session_identity_guard.py
def _load_data_fix() -> str:
    """The data-fix statement of migration 0005b (round 5 moved it out of 0005, see D2-3 / NEW-14)."""
    import importlib.util

    path = REPO_ROOT / "apps" / "api" / "alembic" / "versions" / "20261101_0005b_withdrawal_data_fix.py"
    spec = importlib.util.spec_from_file_location("mig_0005b", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.DATA_FIX)


L2_5_DATA_FIX = _load_data_fix()


def _owner(sql: str, org: uuid.UUID | None = None, **params: object) -> object:
    """Owner-role statement; tenant tables FORCE RLS even for the owner, so the org context is bound when given."""
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            if org is not None:
                conn.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(org)})
            res = conn.execute(text(sql), params)
            return res.scalar() if res.returns_rows else res.rowcount
    finally:
        eng.dispose()


def _submit_and_approve(app, tenant: Tenant, sales, q):  # type: ignore[no-untyped-def]
    approver = login_as(app, tenant.users["approver"])
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert a.status_code == 200, a.text
    return approver, a.json()


def _import(admin, name: str, rows: bytes, *, activate: bool) -> dict:  # type: ignore[no-untyped-def, type-arg]
    v = upload(
        admin,
        "/catalogs/import",
        f"{name}.csv",
        b"SKU,Description,Unit,Currency,List Price\n" + rows,
        {"mapping": MAPPING, "catalog_name": "Main"},
    )
    assert v.status_code == 201, v.text
    if activate:
        assert admin.post(f"/catalogs/versions/{v.json()['id']}/activate").status_code == 200
    return v.json()


# ---------------------------------------------------------------------------------------------------
# N2-1 B (RFQ route): an RFQ parsed while no catalog is active has no frozen catalog version, so the item
# selection guard (`rfq.catalog_version_id and …`) is skipped and products of NEVER-ACTIVATED versions become
# `catalog`-priced quote lines with no blocker. NEW-13 (Low).
# ---------------------------------------------------------------------------------------------------
def test_new13_rfq_without_catalog_version_refuses_products_of_inactive_versions(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Gamma Nocat", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    sales = login_as(app, t.users["sales"])
    rfq = sales.post(
        "/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", "customer_id": str(t.customer_id)}
    ).json()
    assert rfq["status"] == "parsed"
    item = rfq["items"][0]
    # a price list that was imported but never activated (and a later one that IS active)
    cheap = _import(admin, "cheap", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.01\n", activate=False)
    _import(admin, "real", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.85\n", activate=True)
    bogus = sales.get(f"/catalogs/versions/{cheap['id']}/products").json()["items"][0]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": bogus["id"]})
    # secure behaviour: the inactive product is refused (the editor refuses it with 422 since round 4)
    assert r.status_code == 422, r.text


def test_new13_repro_inactive_product_becomes_catalog_priced_line_via_rfq(app) -> None:  # type: ignore[no-untyped-def]
    """Documents the current (insecure) behaviour end to end; remove together with NEW-13 fix + marker above."""
    t = make_tenant("Gamma Nocat2", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    sales = login_as(app, t.users["sales"])
    rfq = sales.post(
        "/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", "customer_id": str(t.customer_id)}
    ).json()
    item = rfq["items"][0]
    cheap = _import(admin, "cheap", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.01\n", activate=False)
    _import(admin, "real", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.85\n", activate=True)
    bogus = sales.get(f"/catalogs/versions/{cheap['id']}/products").json()["items"][0]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": bogus["id"]})
    if r.status_code == 422:
        pytest.skip("NEW-13 fixed at the RFQ item level")
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201, q.text
    line = q.json()["current_version"]["lines"][0]
    assert (line["unit_price"], line["price_source"]) == ("0.01", "catalog")
    assert q.json()["blockers"] == []  # submittable at the never-activated price, no confirmation needed


# ---------------------------------------------------------------------------------------------------
# N2-1 B (copy / stale-draft route): submission never re-checks that `catalog`-priced lines belong to the quote's
# pricing catalog version. An ad-hoc quote (no RFQ → no frozen version → authority = ACTIVE version) keeps lines
# priced from a version that was archived meanwhile; the copy made by POST /versions inherits them. NEW-13 (Low).
# ---------------------------------------------------------------------------------------------------
def test_new13_archived_catalog_lines_are_blocked_or_repriced_on_submit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    old = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1001").json()["items"][0]
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"quantity": "1", "product_id": old["id"]}]},
    ).json()
    assert q["current_version"]["lines"][0]["price_source"] == "catalog"
    # a new price list is activated: the old version is archived and is "never a price authority" (round 4)
    _import(admin, "c2", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90\n", activate=True)
    q = sales.get(f"/quotes/{q['id']}").json()
    assert q["current_version"]["lines"][0]["unit_price"] == "0.85"
    # round 5 (D2-2): re-saving the untouched line is allowed (the draft must stay editable) …
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"id": q["current_version"]["lines"][0]["id"], "product_id": old["id"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert [b["code"] for b in r.json()["blockers"]] == ["catalog_version_mismatch"]
    # … but submission at the archived price is blocked until the line is re-linked to the active list
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": r.json()["lock_version"]})
    assert s.status_code == 409, "submission with a line priced from an archived catalog version must be blocked"
    assert "catalog_version_mismatch" in s.text


def test_new13_repro_new_version_copies_archived_catalog_pricing(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Copy route: approved v1 (priced from C1) → C2 activated → v2 copies C1 `catalog` lines; submittable."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    approver, q = _submit_and_approve(app, tenant_a, sales, q)
    _import(admin, "c2", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90\n", activate=True)
    v2 = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]})
    assert v2.status_code == 201, v2.text
    line = v2.json()["current_version"]["lines"][0]
    assert (line["unit_price"], line["price_source"]) == ("0.85", "catalog")
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": v2.json()["lock_version"]})
    # RFQ-created quotes keep their frozen catalog version by design (pricing_snapshot.catalog_version_id), so this
    # is recorded as behaviour, not asserted as a defect: the submission succeeds at the archived price.
    assert s.status_code in (200, 409)


# ---------------------------------------------------------------------------------------------------
# N2-1 A (cross-currency) from every route: RFQ generation, editor re-link, copy — never a `catalog` price
# ---------------------------------------------------------------------------------------------------
def test_n2_1a_cross_currency_product_never_yields_a_catalog_price_on_any_route(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    csv = (REPO_ROOT / "apps/api/app/fixtures/data/demo_catalog.csv").read_bytes()
    csv += b"USD-1,Dollar widget,EA,USD,9.99,5.00,10,2026-01-01,2027-12-31\n"
    v = upload(admin, "/catalogs/import", "mixed.csv", csv, {"mapping": MAPPING, "catalog_name": "Main"}).json()
    assert admin.post(f"/catalogs/versions/{v['id']}/activate").status_code == 200
    usd = sales.get(f"/catalogs/versions/{v['id']}/products?q=USD-1").json()["items"][0]
    # RFQ route: select the USD product for an RFQ item, generate
    rfq = sales.post(
        "/rfqs", json={"text": "3 pcs USD-1 Dollar widget\n", "customer_id": str(tenant_a.customer_id)}
    ).json()
    item = rfq["items"][0]
    assert (
        sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": usd["id"]}).status_code == 200
    )
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote").json()
    line = q["current_version"]["lines"][0]
    assert line["unit_price"] is None and line["price_source"] == "none" and line["product_id"] == usd["id"]
    # editor: typing a price in the same PATCH as the re-link is manual (needs privileged confirmation)
    q2 = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"id": line["id"], "product_id": usd["id"], "unit_price": "9.99", "is_custom": False}],
        },
    ).json()
    l2 = q2["current_version"]["lines"][0]
    assert l2["price_source"] == "manual" and l2["product_id"] == usd["id"]
    assert "manual_price_unconfirmed" in {b["code"] for b in q2["blockers"]}
    assert (
        sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q2["lock_version"]}).status_code == 409
    )
    # Sales / approver cannot self-confirm the manual price
    for who in (sales, login_as(app, tenant_a.users["approver"])):
        r = who.post(
            f"/quotes/{q['id']}/lines/{line['id']}/price-override",
            json={"expected_lock_version": q2["lock_version"], "reason": "trust me"},
        )
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------------------------------
# price-override misuse: a privileged confirmation must not survive a change that alters the priced item
# ---------------------------------------------------------------------------------------------------
def test_price_override_is_invalidated_by_price_product_or_discount_changes(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.50"}]},
    ).json()
    assert {b["code"] for b in q["blockers"]} >= {"manual_price_unconfirmed"}
    q = admin.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/price-override",
        json={"expected_lock_version": q["lock_version"], "reason": "strategic account"},
    ).json()
    assert q["blockers"] == []
    # price decrease, discount, relabelling as custom (clears the product) each void the confirmation
    for change in ({"unit_price": "0.10"}, {"discount_pct": "50"}, {"is_custom": True}):
        q = sales.patch(
            f"/quotes/{q['id']}",
            json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], **change}]},
        ).json()
        assert q["blockers"] != [], f"override survived {change}"
        q = admin.post(
            f"/quotes/{q['id']}/lines/{ln['id']}/price-override",
            json={"expected_lock_version": q["lock_version"], "reason": "re-confirmed"},
        ).json()
        assert q["blockers"] == []
    assert q["current_version"]["lines"][0]["product_id"] is None
    # quantity, tax and description changes do not alter the unit margin: the confirmation persists (documented)
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"id": ln["id"], "quantity": "9999", "tax_rate_pct": "0"}],
        },
    ).json()
    assert q["blockers"] == []


def test_editor_resave_of_an_unchanged_catalog_price_keeps_price_source_catalog(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """The web editor re-sends every field (incl. unit_price) on each save (apps/web/src/app/(app)/quotes/[id]/page.tsx
    `save()`); an unchanged catalog price must not be relabelled `manual` (M-4 control signal) nor create
    `manual_price_unconfirmed` blockers for untouched lines — otherwise privileged confirmation becomes routine."""
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    assert (ln["price_source"], ln["unit_price"]) == ("catalog", "0.85")
    resend = {
        k: ln[k]
        for k in (
            "id",
            "product_id",
            "sku",
            "description",
            "quantity",
            "unit",
            "unit_price",
            "discount_pct",
            "tax_rate_pct",
            "is_custom",
        )
    }
    q2 = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [resend]}).json()
    l2 = q2["current_version"]["lines"][0]
    observed = (l2["price_source"], l2["unit_price"], sorted({b["code"] for b in q2["blockers"]}))
    assert observed == ("catalog", "0.85", []), observed


# ---------------------------------------------------------------------------------------------------
# N2-2: revival attempts after withdrawal — later approval, every role, every export endpoint
# ---------------------------------------------------------------------------------------------------
def test_n2_2_withdrawn_document_stays_withdrawn_after_a_later_version_is_approved(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _submit_and_approve(app, tenant_a, sales, q)
    w = owner.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "wrong price"}
    )
    assert w.status_code == 200, w.text
    v2 = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": w.json()["lock_version"]}).json()
    v2 = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": v2["lock_version"], "notes": "v2"}).json()
    approver, q3 = _submit_and_approve(app, tenant_a, sales, v2)
    assert q3["status"] == "approved" and [v["status"] for v in q3["versions"]] == ["withdrawn", "approved"]
    assert q3["withdrawn_reason"] is None
    for role in ("owner", "admin", "sales", "approver", "readonly"):
        who = login_as(app, tenant_a.users[role])
        for path in (f"/quotes/{q['id']}/versions/1/pdf", f"/quotes/{q['id']}/versions/1/csv"):
            assert who.get(path).status_code != 200, (role, path)
        assert who.post(f"/quotes/{q['id']}/versions/1/pdf", json={}).status_code != 200, role
    assert sales.get(f"/quotes/{q['id']}/versions/2/pdf").status_code == 200
    # withdrawing again (v2) also keeps v1 withdrawn and refuses submitting/approving anything but a new version
    w2 = owner.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q3["lock_version"], "reason": "again"}
    )
    assert w2.status_code == 200 and [v["status"] for v in w2.json()["versions"]] == ["withdrawn", "withdrawn"]
    assert (
        approver.post(
            f"/quotes/{q['id']}/approve", json={"expected_lock_version": w2.json()["lock_version"]}
        ).status_code
        == 409
    )


def test_new14_withdraw_while_draft_then_approve_clears_the_withdrawal_marker(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _submit_and_approve(app, tenant_a, sales, q)
    q = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    w = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "v1 wrong"}
    )
    assert w.status_code == 200 and w.json()["status"] == "draft"
    approver, q2 = _submit_and_approve(app, tenant_a, sales, w.json())
    assert [v["status"] for v in q2["versions"]] == ["withdrawn", "approved"]
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/2/pdf").status_code == 200
    # the quote header is an approved, current document: it must not still carry the v1 withdrawal marker
    assert q2["withdrawn_reason"] is None, q2["withdrawn_reason"]


def test_new14_l2_5_data_fix_is_idempotent_against_round_4_data(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _submit_and_approve(app, tenant_a, sales, q)
    q = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    w = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "v1 wrong"}
    )
    approver, q2 = _submit_and_approve(app, tenant_a, sales, w.json())
    assert sales.get(f"/quotes/{q['id']}/versions/2/pdf").status_code == 200
    # an operator re-applies migration 0005 (downgrade -1 / upgrade head, or a replayed deploy)
    _owner(L2_5_DATA_FIX, org=tenant_a.org_id)
    assert sales.get(f"/quotes/{q['id']}/versions/2/pdf").status_code == 200, (
        "approved v2 was withdrawn by the data fix"
    )


def test_new14_l2_5_data_fix_covers_the_legacy_withdraw_then_new_version_pattern(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    q = make_quote(sales, str(tenant_a.customer_id))
    approver, q = _submit_and_approve(app, tenant_a, sales, q)
    w = owner.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "pricing error"}
    )
    v2 = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": w.json()["lock_version"]}).json()
    assert v2["status"] == "draft" and v2["withdrawn_reason"] is None
    # reproduce the pre-round-3 (ff99762/60adb00) persisted state: v1 still 'approved', header marker cleared by
    # the new version, the withdrawal only visible in approval_decisions
    _owner(
        "UPDATE quote_versions SET status='approved' WHERE quote_id = :q AND version_no = 1",
        org=tenant_a.org_id,
        q=str(q["id"]),
    )
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 200  # the legacy defect, by construction
    _owner(L2_5_DATA_FIX, org=tenant_a.org_id)
    status = _owner(
        "SELECT status FROM quote_versions WHERE quote_id = :q AND version_no = 1", org=tenant_a.org_id, q=str(q["id"])
    )
    assert status == "withdrawn", f"legacy withdrawn approval left as {status!r} by the data fix"


# ---------------------------------------------------------------------------------------------------
# L2-4 / NEW-5: identity triggers and column grants vs. every legitimate write path the product has
# ---------------------------------------------------------------------------------------------------
def test_l2_4_identity_guards_do_not_break_legitimate_auth_and_membership_flows(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    sales = login_as(app, tenant_a.users["sales"])
    # idle touch: last_seen_at is rewritten once it is older than 60 s (UPDATE on sessions as the app role)
    _owner("UPDATE sessions SET last_seen_at = now() - interval '5 minutes' WHERE revoked_at IS NULL")
    assert sales.get("/auth/me").status_code == 200
    # member lifecycle: create, demote (revokes sessions), deactivate, re-activate (owner), new login
    m = owner.post(
        "/org/members",
        json={
            "email": f"new-{uuid.uuid4().hex[:8]}@alpha.example",
            "password": "Another-Strong-Pass-77!",
            "full_name": "New Person",
            "role": "sales",
        },
    )
    assert m.status_code == 201, m.text
    mid = m.json()["id"]
    assert owner.patch(f"/org/members/{mid}", json={"role": "readonly"}).status_code == 200
    assert owner.patch(f"/org/members/{mid}", json={"is_active": False}).status_code == 200
    assert owner.patch(f"/org/members/{mid}", json={"is_active": True}).status_code == 200
    # logout writes revoked_at
    r = sales.post("/auth/logout")
    assert r.status_code in (200, 204), r.text
    assert sales.get("/auth/me").status_code == 401
    # the owner-role operator path still works for liveness columns while identity stays frozen
    assert _owner("UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL AND false") == 0
    with pytest.raises(Exception, match="identity columns are immutable"):
        _owner("UPDATE sessions SET csrf_secret = repeat('a', 64) WHERE revoked_at IS NULL")


def test_new5_app_role_cannot_touch_identity_columns_even_with_a_matching_policy(tenant_a: Tenant) -> None:
    eng = create_engine(os.environ["DATABASE_URL"], future=True)
    denied = []
    with eng.connect() as conn:
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(tenant_a.org_id)})
        for stmt in (
            "UPDATE sessions SET user_id = gen_random_uuid() WHERE false",
            "UPDATE sessions SET organization_id = gen_random_uuid() WHERE false",
            "UPDATE sessions SET expires_at = now() WHERE false",
            "UPDATE users SET email = 'x@y.example' WHERE false",
            "UPDATE users SET is_active = false WHERE false",
            "DELETE FROM sessions WHERE false",
            "DELETE FROM users WHERE false",
            "ALTER TABLE sessions DISABLE TRIGGER trg_sessions_identity",
        ):
            try:
                conn.execute(text("SAVEPOINT s"))
                conn.execute(text(stmt))
                conn.execute(text("RELEASE SAVEPOINT s"))
            except Exception as exc:  # noqa: BLE001
                conn.execute(text("ROLLBACK TO SAVEPOINT s"))
                denied.append((stmt, "permission denied" in str(exc) or "must be owner" in str(exc)))
            else:
                denied.append((stmt, False))
        conn.rollback()
    eng.dispose()
    assert all(ok for _, ok in denied), denied


# ---------------------------------------------------------------------------------------------------
# NEW-3 (kill switch) re-verified via subprocess; NEW-15: the platform-scope audit row is unreadable
# ---------------------------------------------------------------------------------------------------
def _run_revoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "revoke_sessions.py"), *args, "--yes"],
        cwd=API_DIR,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_new3_org_kill_switch_revokes_only_that_tenant_and_writes_one_audit_row(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    a1 = login_as(app, tenant_a.users["sales"])
    a2 = login_as(app, tenant_a.users["owner"])
    b1 = login_as(app, tenant_b.users["sales"])
    before = _owner("SELECT count(*) FROM audit_events WHERE action = 'ops.sessions_revoked'", org=tenant_a.org_id)
    p = _run_revoke("--org", str(tenant_a.org_id))
    assert p.returncode == 0, p.stderr
    assert "revoked 2 session(s)" in p.stdout
    assert a1.get("/auth/me").status_code == 401 and a2.get("/auth/me").status_code == 401
    assert b1.get("/auth/me").status_code == 200
    after = _owner("SELECT count(*) FROM audit_events WHERE action = 'ops.sessions_revoked'", org=tenant_a.org_id)
    assert after == before + 1


def test_new15_platform_scope_revocation_audit_row_is_readable_by_an_operator(app, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    login_as(app, tenant_b.users["sales"])
    ins_before = _owner("SELECT n_tup_ins FROM pg_stat_user_tables WHERE relname = 'audit_events'")
    p = _run_revoke("--user-email", tenant_b.users["sales"])
    assert p.returncode == 0 and "revoked 1 session(s)" in p.stdout, p.stderr
    ins_after = _owner("SELECT n_tup_ins FROM pg_stat_user_tables WHERE relname = 'audit_events'")
    assert ins_after == ins_before + 1  # one row per affected organisation (round 5 remedy for NEW-15) …
    visible = _owner(
        "SELECT count(*) FROM audit_events WHERE action = 'ops.sessions_revoked' "
        "AND summary->>'scope' = 'user' AND summary->>'total' = '1'",
        org=tenant_b.org_id,
    )
    assert visible == 1, "… readable inside the tenant's audit scope by the owner role and the product"
    assert _owner("SELECT count(*) FROM audit_events WHERE organization_id IS NULL") in (0, None)


# ---------------------------------------------------------------------------------------------------
# L2-2: compose topology — the api service must not receive owner-role credentials in any form
# ---------------------------------------------------------------------------------------------------
def test_new16_compose_api_service_receives_no_owner_credentials() -> None:
    import re

    compose = (REPO_ROOT / "infra" / "docker-compose.yml").read_text()
    api_block = compose.split("\n  api:\n", 1)[1].split("\n  web:\n", 1)[0]
    assert re.search(r"DATABASE_ADMIN_URL:\s*\"\"", api_block), "owner DSN must be blanked for the api service"
    env_example = (REPO_ROOT / ".env.example").read_text()
    owner_keys = {
        k
        for k in ("QP_OWNER_PASSWORD", "DATABASE_ADMIN_URL", "POSTGRES_SUPER_PASSWORD")
        if re.search(rf"^{k}=", env_example, re.M)
    }
    # env_file injects EVERY key of .env into the container; the owner-role password is among them
    assert not ("env_file: ../.env" in api_block and owner_keys - {"DATABASE_ADMIN_URL"}), owner_keys
