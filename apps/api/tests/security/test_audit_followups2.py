"""Security re-verification tests (Stage 1 fix rounds 1+2, 2026-11-01).

Companion to ``test_audit_followups.py``. Each test names the finding / ASVS 5.0 item it covers
(see docs/evidence/security-stage1-reverify-2026-11-01.md). Tests that reproduce an OPEN finding are marked
(markers removed once each finding was fixed; the tests now assert the fixed behaviour and fail hard if a defect
failure ("XPASS strict") the moment the defect is fixed, so the marker must be removed together with the fix.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select, text

from app.core.security import generate_token, hash_token
from app.db.base import utcnow
from app.db.session import get_sessionmaker, set_auth_scope, set_tenant
from app.models.org import User, UserSession
from tests.conftest import API, API_DIR, PASSWORD, Tenant, login_as
from tests.integration.test_quotes import make_quote

pytestmark = [pytest.mark.integration, pytest.mark.security]

REPO_ROOT = API_DIR.parents[1]


def _owner_exec(sql: str, org: uuid.UUID | None = None, **params: object) -> None:
    """Owner-role statement; tenant tables FORCE RLS even for the owner, so an org context is bound when given."""
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        if org is not None:
            conn.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(org)})
            params = {**params, "org": str(org)}
        conn.execute(text(sql), params)
    eng.dispose()


def _user_id(email: str) -> uuid.UUID:
    with get_sessionmaker()() as db:
        set_auth_scope(db, login_email=email)
        uid = db.scalar(select(User.id).where(User.email == email))
        assert uid is not None
        return uid


def _approved_quote(app, tenant: Tenant, text_: str | None = None):  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id), text_)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    return sales, approver, a.json()


# ---------------------------------------------------------------------------------------------------
# NEW-1 (Medium): a withdrawn approved document becomes exportable again after a new version (S1-F15, H-1 follow-up)
# ---------------------------------------------------------------------------------------------------
def test_new1_withdrawn_approved_document_is_not_revived_by_a_new_version(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = _approved_quote(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    w = owner.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "pricing error"}
    )
    assert w.status_code == 200, w.text
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409  # withdrawn → not exportable (fixed)
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": w.json()["lock_version"]})
    assert nv.status_code == 201, nv.text
    # the withdrawal decision must keep the v1 document out of circulation; a draft v2 does not un-withdraw v1
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 409
    assert sales.post(f"/quotes/{q['id']}/versions/1/pdf", json={}).status_code == 409


# ---------------------------------------------------------------------------------------------------
# NEW-2 (Medium, availability of the documented Docker topology): Dockerfile CMD is no longer valid exec form
# ---------------------------------------------------------------------------------------------------
def test_new2_dockerfile_cmd_is_valid_exec_form() -> None:
    lines = (API_DIR / "Dockerfile").read_text().splitlines()
    cmd = next(ln for ln in lines if ln.startswith("CMD "))
    # Docker treats a '#' anywhere but column 0 as part of the argument; a non-JSON remainder is run via /bin/sh -c
    argv = json.loads(cmd[len("CMD ") :])
    assert argv[0] == "uvicorn" and "--no-server-header" in argv


# ---------------------------------------------------------------------------------------------------
# NEW-3 (Medium, incident response): scripts/revoke_sessions.py --org fails under FORCE RLS on audit_events
# ---------------------------------------------------------------------------------------------------
def _run_revoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "revoke_sessions.py"), *args, "--yes"],
        env={**os.environ},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_new3_revoke_sessions_script_org_scope_revokes_and_audits(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    a_sales = login_as(app, tenant_a.users["sales"])
    b_sales = login_as(app, tenant_b.users["sales"])
    r = _run_revoke("--org", str(tenant_a.org_id))
    assert r.returncode == 0, r.stderr[-800:]
    assert a_sales.get("/auth/me").status_code == 401  # tenant A sessions revoked …
    assert b_sales.get("/auth/me").status_code == 200  # … tenant B untouched
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        n = db.execute(text("SELECT count(*) FROM audit_events WHERE action = 'ops.sessions_revoked'")).scalar()
    assert n == 1


def test_revoke_sessions_script_user_and_all_scopes_work(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Positive half of NEW-3 (OPERATIONS_RUNBOOK kill switch): --user-email and --all revoke sessions (exit 0)."""
    a_sales = login_as(app, tenant_a.users["sales"])
    a_owner = login_as(app, tenant_a.users["owner"])
    b_sales = login_as(app, tenant_b.users["sales"])
    r = _run_revoke("--user-email", tenant_a.users["sales"].upper())
    assert r.returncode == 0, r.stderr[-800:]
    assert a_sales.get("/auth/me").status_code == 401
    assert a_owner.get("/auth/me").status_code == 200 and b_sales.get("/auth/me").status_code == 200
    r = _run_revoke("--all")
    assert r.returncode == 0, r.stderr[-800:]
    assert a_owner.get("/auth/me").status_code == 401 and b_sales.get("/auth/me").status_code == 401
    assert login_as(app, tenant_b.users["sales"]).get("/auth/me").status_code == 200  # revocation ≠ deactivation


# ---------------------------------------------------------------------------------------------------
# NEW-4 (Medium): a relabelled catalog price bypasses the M-4 manual-price confirmation (S1-A03, X-06)
# ---------------------------------------------------------------------------------------------------
def test_new4_relabelled_catalog_price_bypasses_manual_confirmation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    seed = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    cheap_pid = seed["current_version"]["lines"][0]["product_id"]
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    # step 1: link the cheapest catalog product AND mark the line custom in the same save
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [{"product_id": cheap_pid, "quantity": "1", "is_custom": True, "description": "Compressor 90 kW"}],
        },
    ).json()
    ln = q["current_version"]["lines"][0]
    assert ln["product_id"] is None and ln["is_custom"] is True
    # step 2: now that product_id is gone the SKU can be rewritten too → a "custom" compressor priced like a bolt
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "sku": "COMP-900"}]},
    ).json()
    ln = q["current_version"]["lines"][0]
    assert ln["is_custom"] and ln["product_id"] is None and ln["sku"] == "COMP-900"
    # expected: a custom line can never carry a "catalog" price without a privileged confirmation
    assert "manual_price_unconfirmed" in {b["code"] for b in q["blockers"]}, (ln["price_source"], q["blockers"])
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409


# ---------------------------------------------------------------------------------------------------
# NEW-5 (Low, defence in depth): sessions UPDATE policy lets an org context re-point a session to another user
# ---------------------------------------------------------------------------------------------------
def test_new5_sessions_update_policy_cannot_repoint_a_session_to_another_user(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales_id, owner_id = _user_id(tenant_a.users["sales"]), _user_id(tenant_a.users["owner"])
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, sales_id)
        now = utcnow()
        db.add(
            UserSession(
                user_id=sales_id,
                organization_id=tenant_a.org_id,
                token_hash=hash_token("mine"),
                csrf_secret="c",
                created_at=now,
                expires_at=now + timedelta(days=1),
                last_seen_at=now,
            )
        )
        db.flush()
        # the runtime role only ever sets revoked_at / last_seen_at; identity columns must be immutable.
        # Since migration 0004 this is enforced by column-level grants (permission denied), which is stronger
        # than a 0-row policy miss; both outcomes prove the row cannot be repointed.
        rows_changed = None
        try:
            rows_changed = db.execute(
                text("UPDATE sessions SET user_id = :o WHERE token_hash = :t"),
                {"o": str(owner_id), "t": hash_token("mine")},
            ).rowcount
        except Exception as exc:  # noqa: BLE001
            assert "permission denied" in str(exc)
        db.rollback()
    assert rows_changed in (None, 0)


# ---------------------------------------------------------------------------------------------------
# NEW-6 (Low): admin may re-activate an owner that an owner deactivated (hierarchy asymmetry, ASVS V4.2.2)
# ---------------------------------------------------------------------------------------------------
def test_new6_admin_cannot_reactivate_an_owner(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    r = owner.post(
        "/org/members",
        json={"email": "owner2@alphatrading.example", "full_name": "O2", "role": "owner", "password": PASSWORD},
    )
    assert r.status_code == 201
    mid = r.json()["id"]
    assert owner.patch(f"/org/members/{mid}", json={"is_active": False}).status_code == 200
    assert admin.patch(f"/org/members/{mid}", json={"is_active": True}).status_code == 403


# ---------------------------------------------------------------------------------------------------
# SEC-01 re-verification with a genuinely shared account (ASVS V4.2.1, X-01): activation and revocation per tenant
# ---------------------------------------------------------------------------------------------------
def test_sec01_shared_account_deactivation_and_revocation_are_tenant_scoped(
    app, tenant_a: Tenant, tenant_b: Tenant
) -> None:  # type: ignore[no-untyped-def]
    shared = tenant_a.users["sales"]
    uid = _user_id(shared)
    _owner_exec(
        "INSERT INTO memberships (id, organization_id, user_id, role, is_active, created_at, updated_at) "
        "VALUES (gen_random_uuid(), :org, :uid, 'sales', true, now(), now())",
        org=tenant_b.org_id,
        uid=uid,
    )
    # a tenant-B session for the shared account (no org switcher exists yet, so it is inserted directly)
    b_token = generate_token(32)
    _owner_exec(
        "INSERT INTO sessions (id, user_id, organization_id, token_hash, csrf_secret, created_at, expires_at, "
        "last_seen_at) VALUES (gen_random_uuid(), :uid, :org, :th, 'c', now(), now() + interval '1 day', now())",
        uid=uid,
        org=tenant_b.org_id,
        th=hash_token(b_token),
    )
    a_session = login_as(app, shared)  # oldest active membership → tenant A
    assert a_session.info["organization"]["id"] == str(tenant_a.org_id)
    from fastapi.testclient import TestClient

    b_client = TestClient(app, base_url="http://testserver", cookies={"qp_session": b_token})
    assert b_client.get(f"{API}/auth/me").json()["organization"]["id"] == str(tenant_b.org_id)
    b_owner = login_as(app, tenant_b.users["owner"])
    mid = next(m["id"] for m in b_owner.get("/org/members").json() if m["user"]["email"] == shared)
    assert b_owner.patch(f"/org/members/{mid}", json={"role": "readonly"}).status_code == 200  # revokes B only
    assert b_client.get(f"{API}/auth/me").status_code == 401
    assert a_session.get("/auth/me").status_code == 200
    assert b_owner.patch(f"/org/members/{mid}", json={"is_active": False}).status_code == 200
    assert a_session.get("/auth/me").status_code == 200  # tenant A access survives tenant B deactivation
    assert login_as(app, shared).info["organization"]["id"] == str(tenant_a.org_id)
    with get_sessionmaker()() as db:
        set_auth_scope(db, login_email=shared)
        assert db.scalar(select(User.is_active).where(User.email == shared)) is True  # global flag untouched
    # tenant A sees nothing of the B-side membership state and cannot act on B's membership id
    a_owner = login_as(app, tenant_a.users["owner"])
    assert a_owner.patch(f"/org/members/{mid}", json={"is_active": True}).status_code == 404


# ---------------------------------------------------------------------------------------------------
# L-1 / SEC-02: users & sessions policies — cross-tenant read/write probes as the runtime role (THREAT_MODEL B3/B7)
# ---------------------------------------------------------------------------------------------------
def test_l1_auth_table_policies_block_cross_tenant_reads_and_writes(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    login_as(app, tenant_a.users["sales"])
    login_as(app, tenant_b.users["sales"])
    a_sales = _user_id(tenant_a.users["sales"])
    with get_sessionmaker()() as db:  # tenant A context, no acting user (service/admin paths)
        set_tenant(db, tenant_a.org_id, None)
        assert db.execute(text("SELECT count(*) FROM users WHERE email LIKE '%betasupplies%'")).scalar() == 0
        assert db.execute(text("SELECT count(DISTINCT organization_id) FROM sessions")).scalar() == 1
        assert (
            db.execute(
                text("UPDATE users SET full_name = 'x' WHERE email = :e"), {"e": tenant_b.users["sales"]}
            ).rowcount
            == 0
        )
        assert db.execute(text("UPDATE users SET password_hash = 'x'")).rowcount == 0  # not even own members
        for stmt in ("DELETE FROM users", "DELETE FROM sessions"):  # DELETE is revoked for the runtime role (0004)
            try:
                assert db.execute(text(stmt)).rowcount == 0
            except Exception as exc:  # noqa: BLE001
                assert "permission denied" in str(exc)
                db.rollback()
                set_tenant(db, tenant_a.org_id, None)
        assert (
            db.execute(
                text("UPDATE sessions SET revoked_at = now() WHERE organization_id = :o"), {"o": str(tenant_b.org_id)}
            ).rowcount
            == 0
        )
        assert (
            db.execute(
                text("UPDATE sessions SET revoked_at = revoked_at WHERE organization_id = :o"),
                {"o": str(tenant_a.org_id)},
            ).rowcount
            >= 1
        )
        db.rollback()
    with get_sessionmaker()() as db:  # tenant A acting user may not mint a session for tenant B
        set_tenant(db, tenant_a.org_id, a_sales)
        with pytest.raises(Exception, match="row-level security"):
            db.execute(
                text(
                    "INSERT INTO sessions (id, user_id, organization_id, token_hash, csrf_secret, created_at, expires_at, "
                    "last_seen_at) VALUES (gen_random_uuid(), :u, :o, 'x', 'c', now(), now() + interval '1 day', now())"
                ),
                {"u": str(a_sales), "o": str(tenant_b.org_id)},
            )
        db.rollback()
    with get_sessionmaker()() as db:  # tenant B context cannot revoke tenant A sessions
        set_tenant(db, tenant_b.org_id, None)
        assert (
            db.execute(
                text("UPDATE sessions SET revoked_at = now() WHERE organization_id = :o"), {"o": str(tenant_a.org_id)}
            ).rowcount
            == 0
        )
        db.rollback()
    with get_sessionmaker()() as db:  # login scope: exactly one row, read-only, no memberships, no LIKE wildcards
        set_auth_scope(db, login_email=tenant_b.users["owner"])
        assert db.execute(text("SELECT count(*) FROM users")).scalar() == 1
        assert db.execute(text("UPDATE users SET password_hash = 'x'")).rowcount == 0
        assert db.execute(text("SELECT count(*) FROM memberships")).scalar() == 0
        assert db.execute(text("SELECT count(*) FROM sessions")).scalar() == 0
        db.rollback()
    with get_sessionmaker()() as db:
        set_auth_scope(db, login_email="%")
        assert db.execute(text("SELECT count(*) FROM users")).scalar() == 0
        db.rollback()


# ---------------------------------------------------------------------------------------------------
# SEC-05 / M-2: uvicorn's proxy-header handling with the Dockerfile default (trust 127.0.0.1 only)
# ---------------------------------------------------------------------------------------------------
def test_m2_spoofed_forwarded_for_is_ignored_from_a_non_loopback_peer(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")  # == uvicorn default / Dockerfile CMD

    async def login_from(peer: tuple[str, int]) -> None:
        transport = httpx.ASGITransport(app=wrapped, client=peer)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            pre = (await c.get(f"{API}/auth/csrf")).json()["csrf_token"]
            r = await c.post(
                f"{API}/auth/login",
                json={"email": tenant_a.users["readonly"], "password": PASSWORD},
                headers={"X-CSRF-Token": pre, "X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.10"},
            )
            assert r.status_code == 200, r.text

    def last_login_ip() -> str | None:
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            return db.execute(
                text("SELECT ip FROM audit_events WHERE action = 'auth.login' ORDER BY id DESC LIMIT 1")
            ).scalar()

    asyncio.run(login_from(("10.0.0.5", 40000)))  # e.g. the web container in docker-compose
    assert last_login_ip() == "10.0.0.5"
    asyncio.run(login_from(("127.0.0.1", 40001)))  # loopback peers ARE trusted by default (documented caveat)
    assert last_login_ip() == "203.0.113.9"


# ---------------------------------------------------------------------------------------------------
# M-4: price-override authorisation matrix incl. the legacy alias route and cross-tenant ids (ASVS V4.1.3, V4.2.1)
# ---------------------------------------------------------------------------------------------------
def test_m4_price_override_matrix_and_reset_on_reprice(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    assert ln["price_source"] == "catalog"
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "999"}]},
    ).json()
    assert {b["code"] for b in q["blockers"]} == {"manual_price_unconfirmed"}  # above cost, still needs confirmation
    body = {"expected_lock_version": q["lock_version"], "reason": "verified with supplier"}
    for role, expected in (("sales", 403), ("approver", 403), ("readonly", 403)):
        ac = login_as(app, tenant_a.users[role])
        for route in ("price-override", "margin-override"):
            assert ac.post(f"/quotes/{q['id']}/lines/{ln['id']}/{route}", json=body).status_code == expected, (
                role,
                route,
            )
    b_admin = login_as(app, tenant_b.users["admin"])
    for route in ("price-override", "margin-override"):
        assert b_admin.post(f"/quotes/{q['id']}/lines/{ln['id']}/{route}", json=body).status_code == 404
    assert (
        admin.post(f"/quotes/{q['id']}/lines/{ln['id']}/price-override", json={**body, "reason": "abc"}).status_code
        == 422
    )
    r = admin.post(f"/quotes/{q['id']}/lines/{ln['id']}/price-override", json=body)
    assert r.status_code == 200 and r.json()["blockers"] == []
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": r.json()["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.01"}]},
    ).json()
    codes = {b["code"] for b in q["blockers"]}
    assert codes == {"manual_price_unconfirmed", "below_min_margin"}
    assert q["current_version"]["lines"][0].get("margin_override_reason") is None
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409


# ---------------------------------------------------------------------------------------------------
# SEC-08 / SEC-13 quick re-verification (ASVS V5.1.3, V4.2.2)
# ---------------------------------------------------------------------------------------------------
def test_sec08_nul_bytes_in_every_search_parameter_are_handled(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ro = login_as(app, tenant_a.users["readonly"])
    for path in (
        "/quotes?q=%00",
        "/quotes?status=%00draft",
        "/customers?q=a%00b",
        "/rfqs?q=%00",
        f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=%00",
    ):
        r = ro.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])


def test_sec13_logout_requires_a_valid_csrf_token(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ac = login_as(app, tenant_a.users["readonly"])
    assert ac.client.post(f"{API}/auth/logout").status_code == 403
    assert ac.client.post(f"{API}/auth/logout", headers={"X-CSRF-Token": "bogus"}).status_code == 403
    assert ac.get("/auth/me").status_code == 200  # still logged in
    assert ac.post("/auth/logout").status_code == 200
    assert ac.get("/auth/me").status_code == 401
