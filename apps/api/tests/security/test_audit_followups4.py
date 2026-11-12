"""Security re-verification tests, round 4 (Stage 1 fix rounds 5+6, commit f69ac25, 2026-11-10).

Companion to ``test_audit_followups*.py``. Re-tests NEW-13..NEW-17 and I-1 adversarially and probes the round-5/6
code (``update_draft`` provenance rules, ``stale_catalog_lines``, migration 0005b, ``rfqs.update_item``,
``GET /catalogs/active-version``, ``scripts/revoke_sessions.py``, ``scripts/check_compose.py``, the editor re-link).
Findings that remain open are ``xfail(strict=True)`` with their ID: they fail the suite the moment the defect is
fixed, so remove the marker together with the fix. See docs/evidence/security-stage1-reverify3-2026-11-10.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

from tests.conftest import API_DIR, Tenant, login_as, make_tenant, upload

pytestmark = [pytest.mark.integration, pytest.mark.security]

REPO_ROOT = API_DIR.parents[1]
MAPPING = json.dumps(
    {"sku": "SKU", "description": "Description", "unit": "Unit", "currency": "Currency", "list_price": "List Price"}
)


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


def _owner(sql: str, org: uuid.UUID | None = None, **params: object) -> object:
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            if org is not None:
                conn.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(org)})
            res = conn.execute(text(sql), params)
            return res.scalar() if res.returns_rows else res.rowcount
    finally:
        eng.dispose()


def _adhoc_quote_with_bolt(sales, tenant: Tenant, qty: str = "10"):  # type: ignore[no-untyped-def]
    prod = sales.get(f"/catalogs/versions/{tenant.catalog_version_id}/products?q=SKU-1001").json()["items"][0]
    q = sales.post("/quotes", json={"customer_id": str(tenant.customer_id)}).json()
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"product_id": prod["id"], "quantity": qty}]},
    )
    assert r.status_code == 200, r.text
    line = r.json()["current_version"]["lines"][0]
    assert (line["price_source"], line["unit_price"]) == ("catalog", "0.85")
    return prod, r.json()


def _line_patch(sales, q: dict, **extra):  # type: ignore[no-untyped-def, type-arg]
    ln = q["current_version"]["lines"][0]
    body = {"id": ln["id"], "product_id": ln["product_id"], "quantity": ln["quantity"], "unit_price": ln["unit_price"]}
    body.update(extra)
    return sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [body]})


# ---------------------------------------------------------------------------------------------------
# NEW-17 / D2-1 — provenance rules in update_draft
# ---------------------------------------------------------------------------------------------------
def test_new17_resend_with_trailing_zeros_keeps_catalog_and_non_finite_prices_are_rejected(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    _, q = _adhoc_quote_with_bolt(sales, tenant_a)
    r = _line_patch(sales, q, unit_price="0.850000")
    assert r.status_code == 200, r.text
    assert r.json()["current_version"]["lines"][0]["price_source"] == "catalog" and r.json()["blockers"] == []
    q = r.json()
    for bad in ("NaN", "sNaN", "Infinity", "-Infinity", "1e999", "-1", "-0.01"):
        r = _line_patch(sales, q, unit_price=bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)  # never a 500 from Decimal comparison
    after = sales.get(f"/quotes/{q['id']}").json()["current_version"]["lines"][0]
    assert (after["price_source"], after["unit_price"]) == ("catalog", "0.85")


def test_new17_sending_the_list_price_cannot_launder_a_manual_price_into_catalog(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """A manual (below-list) price typed by sales stays `manual` and unconfirmed even if the list price is re-sent,
    and a client-supplied `price_source` is ignored (the schema has no such field)."""
    sales = login_as(app, tenant_a.users["sales"])
    prod, q = _adhoc_quote_with_bolt(sales, tenant_a)
    q = _line_patch(sales, q, unit_price="0.20").json()
    assert q["current_version"]["lines"][0]["price_source"] == "manual"
    assert "manual_price_unconfirmed" in [b["code"] for b in q["blockers"]]
    for extra in ({"unit_price": prod["list_price"]}, {"unit_price": "0.2", "price_source": "catalog"}):
        r = _line_patch(sales, q, **extra)
        assert r.status_code == 200, r.text
        q = r.json()
        line = q["current_version"]["lines"][0]
        assert line["price_source"] == "manual", extra
        assert "manual_price_unconfirmed" in [b["code"] for b in q["blockers"]]
    # sales cannot submit it; the privileged confirmation is still required
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 409, s.text


def test_new17_stale_price_resent_with_a_relink_becomes_manual_not_catalog(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Re-linking to a product of the new version while re-sending the OLD list price must not keep `catalog`."""
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    _, q = _adhoc_quote_with_bolt(sales, tenant_a)
    v2 = _import(admin, "c2", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90\n", activate=True)
    new = sales.get(f"/catalogs/versions/{v2['id']}/products?q=SKU-1001").json()["items"][0]
    q = sales.get(f"/quotes/{q['id']}").json()
    r = _line_patch(sales, q, product_id=new["id"], unit_price="0.85")
    assert r.status_code == 200, r.text
    line = r.json()["current_version"]["lines"][0]
    assert (line["unit_price"], line["price_source"]) == ("0.85", "manual")
    # the editor's own request shape (no unit_price) re-prices from the catalog
    q = sales.get(f"/quotes/{q['id']}").json()
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"id": ln["id"], "product_id": new["id"], "unit_price": None}],
        },
    )
    line = r.json()["current_version"]["lines"][0]
    assert (line["unit_price"], line["price_source"]) == ("0.9", "catalog"), r.text


# ---------------------------------------------------------------------------------------------------
# NEW-13 — stale-catalog blocker cannot be shed by editing around it; RFQ route; cross-tenant link
# ---------------------------------------------------------------------------------------------------
def test_new13_stale_blocker_survives_reprice_null_quantity_and_new_version_and_clears_on_relink(
    app, tenant_a: Tenant
) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    old, q = _adhoc_quote_with_bolt(sales, tenant_a)
    v2 = _import(admin, "c2", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.90\n", activate=True)
    codes = lambda x: [b["code"] for b in x["blockers"]]  # noqa: E731
    q = sales.get(f"/quotes/{q['id']}").json()
    assert codes(q) == ["catalog_version_mismatch"]
    for extra in ({"unit_price": None}, {"quantity": "3"}, {"discount_pct": "1"}, {"is_custom": False}):
        r = _line_patch(sales, q, **extra)
        assert r.status_code == 200, (extra, r.text)
        q = r.json()
        assert codes(q) == ["catalog_version_mismatch"], extra
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 409 and [b["code"] for b in s.json()["blockers"]] == ["catalog_version_mismatch"]
    # linking back to the archived product by a *new* line is refused outright
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"product_id": old["id"], "quantity": "1"}]},
    )
    assert r.status_code == 422, r.text
    # the editor's relink: new product id, no unit_price → server re-prices
    new = sales.get(f"/catalogs/versions/{v2['id']}/products?q=SKU-1001").json()["items"][0]
    q = sales.get(f"/quotes/{q['id']}").json()
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "product_id": new["id"]}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["blockers"] == []
    assert r.json()["current_version"]["lines"][0]["unit_price"] == "0.9"
    q = r.json()
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s.json()["lock_version"]})
    assert a.status_code == 200, a.text


def test_new13_rfq_item_selected_before_activation_is_flagged_after_it(app) -> None:  # type: ignore[no-untyped-def]
    """RFQ parsed with no catalog (no frozen version): an item chosen from the active list, then a new list is
    activated before generate-quote. The generated ad-hoc quote must not be submittable at the archived price."""
    t = make_tenant("Delta Nocat", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    sales = login_as(app, t.users["sales"])
    rfq = sales.post(
        "/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n", "customer_id": str(t.customer_id)}
    ).json()
    item = rfq["items"][0]
    v1 = _import(admin, "v1", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.85\n", activate=True)
    p1 = sales.get(f"/catalogs/versions/{v1['id']}/products").json()["items"][0]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": p1["id"]})
    assert r.status_code == 200, r.text
    _import(admin, "v2", b"SKU-1001,Hex bolt M8x40 zinc plated,EA,AED,0.95\n", activate=True)
    # now the archived v1 product can no longer be selected on the RFQ either
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": p1["id"]})
    assert r.status_code == 422, r.text
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    if g.status_code == 201:
        q = g.json()
        s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
        assert s.status_code == 409, "a quote priced from an archived list must not be submittable"
    else:
        assert g.status_code in (409, 422), g.text


def test_round6_active_version_endpoint_is_tenant_scoped_authenticated_and_read_only(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    a = login_as(app, tenant_a.users["readonly"])
    b = login_as(app, tenant_b.users["readonly"])
    ra, rb = a.get("/catalogs/active-version"), b.get("/catalogs/active-version")
    assert ra.status_code == 200 and rb.status_code == 200
    assert ra.json() == {"version_id": str(tenant_a.catalog_version_id)}
    assert rb.json() == {"version_id": str(tenant_b.catalog_version_id)}
    assert set(ra.json()) == {"version_id"}  # no names, prices, counts
    # a tenant never gets the other tenant's version content through the listing route
    assert a.get(f"/catalogs/versions/{tenant_b.catalog_version_id}/products").status_code == 404
    from fastapi.testclient import TestClient

    anon = TestClient(app)
    assert anon.get("/api/v1/catalogs/active-version").status_code == 401
    for method in ("post", "patch", "delete"):
        assert getattr(a, method)("/catalogs/active-version").status_code in (403, 405, 422)
    t = make_tenant("Eps Nocat", with_catalog=False)
    assert login_as(app, t.users["readonly"]).get("/catalogs/active-version").json() == {"version_id": None}


def test_round6_quote_line_cannot_link_another_tenants_product(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    foreign = (
        login_as(app, tenant_b.users["sales"])
        .get(f"/catalogs/versions/{tenant_b.catalog_version_id}/products")
        .json()["items"][0]
    )
    _, q = _adhoc_quote_with_bolt(sales, tenant_a)
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "product_id": foreign["id"]}]},
    )
    assert r.status_code == 404, r.text
    assert sales.get(f"/quotes/{q['id']}").json()["current_version"]["lines"][0]["product_id"] == ln["product_id"]


# ---------------------------------------------------------------------------------------------------
# NEW-14 — withdrawal across versions; migration 0005b leaves FORCE RLS in place
# ---------------------------------------------------------------------------------------------------
def test_new14_withdrawn_version_stays_unexportable_after_a_later_version_is_approved(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    _, q = _adhoc_quote_with_bolt(sales, tenant_a)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    q = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]}).json()
    q = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    q = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "wrong terms"}
    ).json()
    assert q["status"] == "draft" and q["withdrawn_reason"] == "wrong terms"
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    q = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]}).json()
    assert q["status"] == "approved" and q["withdrawn_reason"] is None
    assert [(v["version_no"], v["status"]) for v in q["versions"]] == [(1, "withdrawn"), (2, "approved")]
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/2/pdf").status_code == 200
    # a role without withdraw permission cannot reach the withdraw route in this state either
    assert (
        sales.post(
            f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "x"}
        ).status_code
        == 403
    )


def test_new14_migration_0005b_leaves_force_rls_on_every_relaxed_table(app) -> None:  # type: ignore[no-untyped-def]
    flags = {
        t: _owner("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t AND relkind = 'r'", t=t)
        for t in ("quotes", "quote_versions", "approval_decisions", "audit_events")
    }
    assert flags == {k: True for k in flags}, flags
    src = (REPO_ROOT / "apps/api/alembic/versions/20261101_0005b_withdrawal_data_fix.py").read_text()
    # every NO FORCE has a matching FORCE in a `finally` (so a failing data fix cannot leave the owner unrestricted)
    code = src.split('"""', 2)[2]
    assert "finally:" in code and code.count("NO FORCE ROW LEVEL SECURITY") == 1
    assert code.count("FORCE ROW LEVEL SECURITY") == 2  # one NO FORCE and one FORCE statement


def test_new14_data_fix_replay_twice_is_a_no_op_second_time(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mig_0005b_r4", REPO_ROOT / "apps/api/alembic/versions/20261101_0005b_withdrawal_data_fix.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True)
    try:
        with eng.begin() as conn:
            for t in mod.TABLES:
                conn.execute(text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
            try:
                first = conn.execute(text(mod.DATA_FIX)).rowcount
                second = conn.execute(text(mod.DATA_FIX)).rowcount
            finally:
                for t in mod.TABLES:
                    conn.execute(text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY"))
        assert (first, second) == (0, 0)  # clean database: nothing to withdraw, nothing re-withdrawn
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------------------------------
# NEW-15 — kill-switch audit rows
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


def test_new15_all_scope_writes_one_readable_row_per_org_with_that_orgs_count_and_no_null_org_rows(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    login_as(app, tenant_a.users["sales"])
    login_as(app, tenant_a.users["owner"])
    login_as(app, tenant_b.users["sales"])
    p = _run_revoke("--all")
    assert p.returncode == 0 and "revoked 3 session(s)" in p.stdout, p.stderr
    q = "SELECT summary->>'count' FROM audit_events WHERE action = 'ops.sessions_revoked'"
    assert _owner(q, org=tenant_a.org_id) == "2"
    assert _owner(q, org=tenant_b.org_id) == "1"
    assert _owner("SELECT count(*) FROM audit_events WHERE organization_id IS NULL") == 0


def test_new15_malicious_org_argument_is_parameterised_and_revokes_nothing(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    c = login_as(app, tenant_a.users["sales"])
    p = _run_revoke("--org", "x' OR '1'='1")
    assert p.returncode != 0
    assert c.get("/auth/me").status_code == 200  # nothing was revoked by the injection attempt


def test_r4_1_tenant_audit_row_does_not_disclose_platform_wide_session_totals(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    login_as(app, tenant_a.users["sales"])
    login_as(app, tenant_b.users["sales"])
    login_as(app, tenant_b.users["owner"])
    assert _run_revoke("--all").returncode == 0
    summary = _owner(
        "SELECT summary::text FROM audit_events WHERE action = 'ops.sessions_revoked'", org=tenant_a.org_id
    )
    assert "total" not in json.loads(str(summary)), summary  # tenant A's row currently carries total=3 (B's count)


# ---------------------------------------------------------------------------------------------------
# NEW-16 / R3-3 — compose topology and the check script
# ---------------------------------------------------------------------------------------------------
COMPOSE = REPO_ROOT / "infra" / "docker-compose.yml"
CHECK = REPO_ROOT / "scripts" / "check_compose.py"


def _check(tmp_path: Path, doc: dict) -> subprocess.CompletedProcess[str]:  # type: ignore[type-arg]
    f = tmp_path / "rendered.yml"
    f.write_text(yaml.safe_dump(doc))
    return subprocess.run([sys.executable, str(CHECK), str(f)], capture_output=True, text=True, check=False)


def test_new16_compose_source_gives_api_and_web_no_env_file_and_no_owner_material() -> None:
    doc = yaml.safe_load(COMPOSE.read_text())
    for name in ("api", "web"):
        svc = doc["services"][name]
        assert "env_file" not in svc, name
        blob = json.dumps(svc.get("environment") or {})
        assert "OWNER" not in blob.upper() and "quotepilot_owner" not in blob, name
    assert doc["services"]["api"]["environment"]["DATABASE_ADMIN_URL"] == ""
    assert "env_file" in doc["services"]["migrate"]  # the only service that legitimately holds the owner DSN


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_new16_rendered_compose_as_documented_has_no_owner_credentials_in_api_or_web(tmp_path: Path) -> None:
    root_env = REPO_ROOT / ".env"
    if not root_env.exists():
        pytest.skip("migrate service needs ../.env; copy .env.example to .env to run this check")
    out = subprocess.run(
        ["docker", "compose", "--env-file", ".env", "-f", "infra/docker-compose.yml", "config"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    rendered = yaml.safe_load(out.stdout)
    for name in ("api", "web"):
        env = rendered["services"][name].get("environment") or {}
        assert "QP_OWNER_PASSWORD" not in env and not env.get("DATABASE_ADMIN_URL"), name
        assert "quotepilot_owner" not in json.dumps(env), name
    assert _check(tmp_path, rendered).returncode == 0


def test_new16_check_script_detects_env_file_owner_password_and_owner_dsn(tmp_path: Path) -> None:
    base = {"services": {"api": {"environment": {"X": "1"}}, "web": {"environment": {"API_INTERNAL_URL": "http://a"}}}}
    assert _check(tmp_path, base).returncode == 0
    for mutate in (
        lambda d: d["services"]["api"].update(env_file=["../.env"]),
        lambda d: d["services"]["api"]["environment"].update(QP_OWNER_PASSWORD="x"),
        lambda d: d["services"]["web"]["environment"].update(DB="postgresql://quotepilot_owner:pw@h/db"),
        lambda d: d["services"]["api"]["environment"].update(DATABASE_ADMIN_URL="postgresql://o:p@h/db"),
    ):
        d = yaml.safe_load(yaml.safe_dump(base))
        mutate(d)
        assert _check(tmp_path, d).returncode == 1, d


def test_r4_2_check_script_fails_when_the_api_service_is_missing_from_the_rendered_file(tmp_path: Path) -> None:
    """R4-2 fixed in round 7 (strict xfail removed): a renamed or missing api/web service fails the check."""
    renamed = {"services": {"backend": {"env_file": ["../.env"], "environment": {"QP_OWNER_PASSWORD": "x"}}}}
    assert _check(tmp_path, renamed).returncode != 0


# ---------------------------------------------------------------------------------------------------
# I-1 and the editor re-link (static: the web tier is covered by the live probes in the evidence file)
# ---------------------------------------------------------------------------------------------------
def test_i1_proxy_drops_x_forwarded_port_and_the_api_trusts_no_other_forwarding_header() -> None:
    src = (REPO_ROOT / "apps/web/src/app/api/[...path]/route.ts").read_text()
    m = re.search(r"const FORWARDING = new Set\(\[(.*?)\]\)", src, re.S)
    assert m is not None
    names = set(re.findall(r'"([a-z-]+)"', m.group(1)))
    assert {
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-real-ip",
        "forwarded",
    } <= names
    app_src = "\n".join(p.read_text() for p in (API_DIR / "app").rglob("*.py"))
    for hdr in ("x-forwarded-prefix", "x-forwarded-port", "x-real-ip", "true-client-ip", "cf-connecting-ip"):
        assert hdr not in app_src.lower(), hdr  # no consumer for the header classes the blocklist does not cover


def test_round6_editor_relink_sends_no_browser_computed_price() -> None:
    src = (REPO_ROOT / "apps/web/src/app/(app)/quotes/[id]/page.tsx").read_text()
    fn = src[src.index("async function relinkToActiveCatalog") : src.index("const approvedVersions")]
    assert "price_touched: false" in fn and 'price_source: "catalog"' in fn
    # the save payload omits unit_price for untouched catalog lines, so the server prices the re-linked line
    assert 'l.price_source === "catalog" && !l.price_touched ? undefined' in src
    assert "provider" not in fn.lower() and "apiKey" not in fn
