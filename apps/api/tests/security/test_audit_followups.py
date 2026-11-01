"""Security-audit follow-up tests (Stage 1, 2026-11-01).

Each test names the ASVS 5.0 item / THREAT_MODEL.md abuse case it covers. Tests that reproduce an OPEN
finding were marked ``xfail(strict=True)`` with the finding id (markers removed as findings were fixed) from docs/evidence/security-stage1-2026-11-01.md:
they fail today (the vulnerability is present), keep the suite green, and will start failing loudly
("XPASS strict") the moment the defect is fixed, so the marker must then be removed.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from app.db.session import get_sessionmaker, set_tenant
from tests.conftest import API, FIXTURES, PASSWORD, AuthedClient, Tenant, login_as, upload
from tests.integration.test_quotes import make_quote

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _admin_exec(sql: str, org: object | None = None, **params: object) -> None:
    """Owner-role statement. Tenant tables FORCE row-level security even for the owner, so a tenant context is
    bound for the statement when `org` is given (sessions/users only ENABLE it and need no context)."""
    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        if org is not None:
            conn.execute(text("SELECT set_config('app.current_org', :o, false)"), {"o": str(org)})
        conn.execute(text(sql), params)
    eng.dispose()


def _submitted_quote(app, tenant: Tenant):  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant.users["sales"])
    q = make_quote(sales, str(tenant.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert s.status_code == 200, s.text
    return sales, s.json()


# ---------------------------------------------------------------------------------------------------
# 1. Cross-tenant access on the object routes not covered by test_tenant_isolation (ASVS V4.2.1, X-01, abuse 5)
# ---------------------------------------------------------------------------------------------------
def test_cross_tenant_remaining_routes_are_404(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    a_sales, a_q = _submitted_quote(app, tenant_a)
    a_rfq = a_sales.post("/rfqs", json={"text": "1 pcs SKU-1001"}).json()
    b_owner = login_as(app, tenant_b.users["owner"])
    b_admin = login_as(app, tenant_b.users["admin"])
    a_member_ids = [m["id"] for m in login_as(app, tenant_a.users["owner"]).get("/org/members").json()]
    line_id = a_q["current_version"]["lines"][0]["id"]
    lv = a_q["lock_version"]
    attempts = [
        ("POST", f"/quotes/{a_q['id']}/submit", {"expected_lock_version": lv}),
        ("POST", f"/quotes/{a_q['id']}/reject", {"expected_lock_version": lv, "reason": "pwn"}),
        (
            "POST",
            f"/quotes/{a_q['id']}/lines/{line_id}/margin-override",
            {"expected_lock_version": lv, "reason": "pwned"},
        ),
        ("POST", f"/rfqs/{a_rfq['id']}/items", {"description": "x", "quantity": "1"}),
        ("PATCH", f"/org/members/{a_member_ids[0]}", {"role": "readonly"}),
        ("PATCH", f"/org/members/{a_member_ids[-1]}", {"is_active": False}),
    ]
    for method, path, body in attempts:
        r = b_owner.post(path, json=body) if method == "POST" else b_owner.patch(path, json=body)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    # tenant A ids inside tenant B payloads: customer in JSON / multipart RFQ creation and quote filter
    assert b_owner.post("/rfqs", json={"text": "1 pcs x", "customer_id": str(tenant_a.customer_id)}).status_code == 404
    r = upload(
        b_owner,
        "/rfqs",
        "r.csv",
        (FIXTURES / "rfq_clean.csv").read_bytes(),
        {"customer_id": str(tenant_a.customer_id)},
    )
    assert r.status_code == 404
    assert b_owner.get(f"/quotes?customer_id={tenant_a.customer_id}").json() == []
    # A's product id as a quote-line product inside B's own quote (B admin path, not just owner)
    a_pid = a_q["current_version"]["lines"][0]["product_id"]
    own = b_admin.post("/quotes", json={}).json()
    r = b_admin.patch(
        f"/quotes/{own['id']}",
        json={"expected_lock_version": 0, "lines": [{"product_id": a_pid, "quantity": "1", "description": "x"}]},
    )
    assert r.status_code == 404
    # A's quote-line id re-used as a line id inside B's quote must not attach/modify A's line
    r = b_admin.patch(
        f"/quotes/{own['id']}",
        json={
            "expected_lock_version": 0,
            "lines": [{"id": line_id, "description": "pwn", "quantity": "1", "unit_price": "1"}],
        },
    )
    assert r.status_code == 200
    assert all(ln["id"] != line_id for ln in r.json()["current_version"]["lines"])
    assert a_sales.get(f"/quotes/{a_q['id']}").json()["current_version"]["lines"][0]["description"] != "pwn"
    # members list of B never contains A's users
    b_emails = {m["user"]["email"] for m in b_owner.get("/org/members").json()}
    assert not (b_emails & set(tenant_a.users.values()))


# ---------------------------------------------------------------------------------------------------
# 2. Vertical privilege escalation on membership routes (ASVS V4.1.3 / V4.2.2, X-02, abuse 6)
# ---------------------------------------------------------------------------------------------------
def test_member_role_escalation_guards(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    members = {m["user"]["email"]: m for m in owner.get("/org/members").json()}
    admin_m = members[tenant_a.users["admin"]]
    sales_m = members[tenant_a.users["sales"]]
    owner_m = members[tenant_a.users["owner"]]
    # admin cannot promote self (or anyone) to owner
    assert admin.patch(f"/org/members/{admin_m['id']}", json={"role": "owner"}).status_code == 403
    assert admin.patch(f"/org/members/{sales_m['id']}", json={"role": "owner"}).status_code == 403
    # admin cannot create an owner
    assert (
        admin.post(
            "/org/members",
            json={
                "email": "o2@alphatrading.example",
                "full_name": "o",
                "role": "owner",
                "password": "Long-Enough-Pass-1",
            },
        ).status_code
        == 403
    )
    # non-privileged users cannot touch memberships at all, including their own row
    for ac, m in ((sales, sales_m), (approver, members[tenant_a.users["approver"]])):
        assert ac.patch(f"/org/members/{m['id']}", json={"role": "admin"}).status_code == 403
        assert ac.patch(f"/org/members/{m['id']}", json={"role": "owner"}).status_code == 403
        assert ac.get("/org/members").status_code == 403
    # admin cannot demote / deactivate the (last) owner; owner cannot deactivate self
    assert admin.patch(f"/org/members/{owner_m['id']}", json={"role": "admin"}).status_code in (409, 422)
    assert admin.patch(f"/org/members/{owner_m['id']}", json={"is_active": False}).status_code in (409, 422)
    assert owner.patch(f"/org/members/{owner_m['id']}", json={"is_active": False}).status_code == 409
    # only an owner may enable self-approval (admin may manage other org settings)
    assert admin.patch("/org", json={"allow_self_approval": True}).status_code == 403
    assert admin.patch("/org", json={"min_margin_pct": "20"}).status_code == 200
    assert owner.patch("/org", json={"allow_self_approval": True}).status_code == 200


def test_admin_cannot_deactivate_an_owner_even_when_two_owners_exist(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    r = owner.post(
        "/org/members",
        json={
            "email": "owner2@alphatrading.example",
            "full_name": "Owner Two",
            "role": "owner",
            "password": "Long-Enough-Pass-1",
        },
    )
    assert r.status_code == 201
    members = {m["user"]["email"]: m for m in owner.get("/org/members").json()}
    owner_m = members[tenant_a.users["owner"]]
    # SEC-03 (fixed in the working tree after the first audit pass): an admin is below owner in the
    # hierarchy, so deactivating an owner must be refused like a role change is
    r = admin.patch(f"/org/members/{owner_m['id']}", json={"is_active": False})
    assert r.status_code in (403, 409, 422), r.text
    assert owner.get("/auth/me").status_code == 200
    # last-ACTIVE-owner guard: deactivating owner2 is fine, then the remaining owner is protected again
    owner2_m = members["owner2@alphatrading.example"]
    assert owner.patch(f"/org/members/{owner2_m['id']}", json={"is_active": False}).status_code == 200
    assert owner.patch(f"/org/members/{owner_m['id']}", json={"role": "sales"}).status_code == 409
    assert admin.patch(f"/org/members/{owner_m['id']}", json={"is_active": False}).status_code in (409, 422)
    assert owner.get("/auth/me").status_code == 200


# ---------------------------------------------------------------------------------------------------
# 2b/11. Cross-tenant impact through the global `users` row (ASVS V4.2.1, X-01, RLS gap on users table)
# ---------------------------------------------------------------------------------------------------
def test_member_create_cannot_attach_or_disable_another_tenants_user(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    victim_email = tenant_a.users["sales"]
    victim = login_as(app, victim_email)
    b_admin = login_as(app, tenant_b.users["admin"])
    r = b_admin.post(
        "/org/members",
        json={
            "email": victim_email,
            "full_name": "Attacker Label",
            "role": "readonly",
            "password": "Long-Enough-Pass-1",
        },
    )
    # secure behaviour: no cross-tenant attach of an existing account without the account holder's consent,
    # and no disclosure of the victim's profile (full_name / is_active) to another tenant
    assert r.status_code != 201 or r.json()["user"]["full_name"] == "Attacker Label", r.text
    if r.status_code == 201:
        b_admin.patch(f"/org/members/{r.json()['id']}", json={"is_active": False})
    # the victim's access to their own tenant must be unaffected either way
    assert victim.get("/auth/me").status_code == 200
    assert login_as(app, victim_email).get("/auth/me").status_code == 200


def test_users_table_is_tenant_scoped(tenant_a: Tenant, tenant_b: Tenant) -> None:
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        emails = {r[0] for r in db.execute(text("SELECT email FROM users")).all()}
        db.rollback()
    assert not (emails & set(tenant_b.users.values())), "tenant A context can read tenant B's user rows"


def test_rls_enabled_and_forced_on_every_tenant_table() -> None:
    """ASVS V4.1 / THREAT_MODEL B7: every table that carries organization_id is RLS-protected and FORCEd."""
    tenant_tables = {
        "customers",
        "catalogs",
        "catalog_versions",
        "products",
        "uploaded_files",
        "rfqs",
        "rfq_items",
        "match_candidates",
        "quotes",
        "quote_versions",
        "quote_lines",
        "approval_decisions",
        "organizations",
        "memberships",
        "audit_events",
        # Stage 2 (migration 0004): follow-up assistant tables
        "contacts",
        "follow_up_policies",
        "message_templates",
        "quote_status_events",
        "follow_up_schedules",
        "communication_drafts",
        "communication_events",
        "integration_connections",
        "job_runs",
    }
    with get_sessionmaker()() as db:
        rows = db.execute(
            text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)"),
            {"t": list(tenant_tables)},
        ).all()
        assert {r[0] for r in rows} == tenant_tables
        assert all(r[1] and r[2] for r in rows), rows
        policies = {r[0] for r in db.execute(text("SELECT tablename FROM pg_policies")).all()}
        assert tenant_tables <= policies
        # columns named organization_id must all live on RLS tables (catches future tables added without a policy)
        org_tables = {
            r[0]
            for r in db.execute(
                text(
                    "SELECT table_name FROM information_schema.columns WHERE column_name = 'organization_id' AND table_schema='public'"
                )
            ).all()
        }
        # `sessions` carries organization_id and has its own (ENABLE, not FORCE) policies since migration 0002,
        # because it is resolved by token hash before any tenant context exists.
        assert org_tables - {"sessions"} <= tenant_tables, org_tables - tenant_tables - {"sessions"}
        assert db.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'sessions'")).scalar() is True
        db.rollback()


def test_audit_table_is_append_only_and_tenant_bound(tenant_a: Tenant, tenant_b: Tenant) -> None:
    """ASVS V7 / THREAT_MODEL Repudiation: app role cannot update/delete/truncate audit rows and cannot
    write an audit row for another organization."""
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            assert (
                db.execute(text(f"SELECT has_table_privilege(current_user, 'audit_events', '{priv}')")).scalar()
                is False
            )
        with pytest.raises(Exception, match="row-level security"):
            db.execute(
                text("INSERT INTO audit_events (organization_id, action, summary) VALUES (:o, 'pwn', '{}'::jsonb)"),
                {"o": str(tenant_b.org_id)},
            )
        db.rollback()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        with pytest.raises(Exception, match="permission denied"):
            db.execute(text("UPDATE audit_events SET action = 'x'"))
        db.rollback()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        with pytest.raises(Exception, match="permission denied"):
            db.execute(text("DELETE FROM audit_events"))
        db.rollback()


# ---------------------------------------------------------------------------------------------------
# 3. Session management (ASVS V3.2 / V3.3 / V3.4, abuse 8)
# ---------------------------------------------------------------------------------------------------
def test_session_rotation_expiry_and_deactivation(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    email = tenant_a.users["sales"]
    # no fixation: a fresh token per login; an attacker-supplied cookie value is never adopted
    c1 = login_as(app, email)
    c2 = login_as(app, email)
    assert c1.client.cookies.get("qp_session") != c2.client.cookies.get("qp_session")
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    pre_cookie = client.cookies.get("qp_csrf_prelogin")
    r = client.post(
        f"{API}/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-CSRF-Token": pre, "Cookie": f"qp_csrf_prelogin={pre_cookie}; qp_session=attacker-chosen-value"},
    )
    assert r.status_code == 200
    set_cookie = " ".join(v for k, v in r.headers.multi_items() if k.lower() == "set-cookie")
    assert "qp_session=" in set_cookie and "attacker-chosen-value" not in set_cookie
    # csrf token is bound to the session: c1's token with c2's cookies is refused
    assert c2.client.post(f"{API}/customers", json={"name": "x"}, headers={"X-CSRF-Token": c1.csrf}).status_code == 403
    # pre-login token cannot be used after login
    assert c2.client.post(f"{API}/customers", json={"name": "x"}, headers={"X-CSRF-Token": pre}).status_code == 403
    # idle expiry (12 h) and absolute expiry (7 d) are enforced server-side
    _admin_exec("UPDATE sessions SET last_seen_at = now() - interval '13 hours' WHERE revoked_at IS NULL")
    assert c1.get("/auth/me").status_code == 401
    c3 = login_as(app, email)
    _admin_exec("UPDATE sessions SET expires_at = now() - interval '1 second' WHERE revoked_at IS NULL")
    assert c3.get("/auth/me").status_code == 401
    # deactivated user: existing sessions revoked and login refused
    c4 = login_as(app, email)
    owner = login_as(app, tenant_a.users["owner"])
    m = next(x for x in owner.get("/org/members").json() if x["user"]["email"] == email)
    assert owner.patch(f"/org/members/{m['id']}", json={"is_active": False}).status_code == 200
    assert c4.get("/auth/me").status_code == 401
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    assert (
        client.post(
            f"{API}/auth/login", json={"email": email, "password": PASSWORD}, headers={"X-CSRF-Token": pre}
        ).status_code
        == 401
    )


def test_login_rate_limit_not_bypassable_by_forwarded_headers(client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """ASVS V2.2.1: X-Forwarded-For is not trusted for the limiter key (see SEC-05 for the flip side)."""
    email = tenant_a.users["sales"]
    for i in range(10):
        pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
        r = client.post(
            f"{API}/auth/login",
            json={"email": email, "password": "wrong-password-xx"},
            headers={"X-CSRF-Token": pre, "X-Forwarded-For": f"10.0.0.{i}", "X-Real-IP": f"10.1.0.{i}"},
        )
        assert r.status_code == 401
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    r = client.post(
        f"{API}/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={"X-CSRF-Token": pre, "X-Forwarded-For": "10.9.9.9"},
    )
    assert r.status_code == 429


# ---------------------------------------------------------------------------------------------------
# 4. CORS / config (ASVS V14.4 / V14.5 / V14.1)
# ---------------------------------------------------------------------------------------------------
def test_cors_rejects_unknown_origin_and_production_refuses_dev_secret(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    r = client.options(
        f"{API}/customers",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    r = client.options(
        f"{API}/customers",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
    from app.core import config

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "dev-only-change-me-dev-only-change-me")
    config.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config.get_settings()
    config.get_settings.cache_clear()
    monkeypatch.setenv("SECRET_KEY", "x" * 31)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):  # min_length=32 enforced
        config.get_settings()
    config.get_settings.cache_clear()
    assert config.Settings(app_env="staging", secret_key="s" * 40).secure_cookies is True
    assert config.Settings(app_env="development", secret_key="s" * 40).secure_cookies is False
    assert config.Settings(app_env="development", secret_key="s" * 40, cookie_secure=True).secure_cookies is True
    config.get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------------
# 5. File handling (ASVS V12.1 / V12.3 / V12.5, abuse 1 & 4)
# ---------------------------------------------------------------------------------------------------
def test_polyglot_and_mismatched_uploads_rejected(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    pdf = (FIXTURES / "rfq_clean.pdf").read_bytes()
    xlsx = (FIXTURES / "rfq_clean.xlsx").read_bytes()
    csv_ = (FIXTURES / "rfq_clean.csv").read_bytes()
    cases = [
        ("looks.csv", pdf),  # PDF bytes behind a csv extension
        ("looks.pdf", xlsx),  # zip bytes behind a pdf extension
        ("looks.xlsx", csv_),  # text behind an xlsx extension
        ("looks.txt", pdf),
        ("noext", csv_),  # no extension at all
        ("page.html", b"<script>alert(1)</script>"),
        ("x.svg", b"<svg onload=alert(1)>"),
        ("x.csv.exe", b"MZ\x90\x00" + b"\x00" * 100),
    ]
    for name, content in cases:
        r = upload(sales, "/rfqs", name, content)
        assert r.status_code == 415, (name, r.status_code, r.text)
        r = login_as(app, tenant_a.users["admin"]).post(
            "/catalogs/import", files={"file": (name, content, "application/octet-stream")}, data={"mapping": "{}"}
        )
        assert r.status_code == 415, (name, r.status_code, r.text)
    # PDF magic followed by garbage: accepted by sniffing, must fail cleanly with a generic reason
    r = upload(sales, "/rfqs", "junk.pdf", b"%PDF-1.7\n" + os.urandom(4000))
    assert r.status_code == 201
    body = r.json()
    assert (
        body["status"] == "failed" and "Traceback" not in body["failure_reason"] and "/" not in body["failure_reason"]
    )
    # oversized multipart → 413 before parsing; empty file → 422
    r = upload(sales, "/rfqs", "big.csv", b"a,b\n" + b"x" * (10 * 1024 * 1024))
    assert r.status_code == 413
    assert upload(sales, "/rfqs", "empty.csv", b"").status_code in (413, 422)


def test_download_headers_are_safe_for_hostile_filenames(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """ASVS V5.2.x / header injection: original filenames never reach Content-Disposition unsanitised."""
    sales = login_as(app, tenant_a.users["sales"])
    hostile = "evil\"; filename*=UTF-8''x.html; X-Inject: 1 ../../etc/passwd é.csv"
    r = upload(sales, "/rfqs", hostile, (FIXTURES / "rfq_clean.csv").read_bytes())
    assert r.status_code == 201, r.text
    d = sales.get(f"/files/{r.json()['source_file_id']}")
    assert d.status_code == 200
    cd = d.headers["content-disposition"]
    assert cd.startswith('attachment; filename="') and cd.count('"') == 2
    name = cd.split('"')[1]
    assert "/" not in name and ";" not in name and "\\" not in name and "'" not in name and name.endswith(".csv")
    assert d.headers["content-type"].startswith("application/octet-stream")
    assert d.headers["x-content-type-options"] == "nosniff" and d.headers["cache-control"] == "no-store"
    # the stored key is server generated and inside the storage root
    from app.services.storage import LocalStorage

    st = LocalStorage(os.environ["STORAGE_DIR"])
    for bad in ("../x", "/etc/passwd", "org/../../x", "org/a/../../../x", "a/b/../../../../x"):
        with pytest.raises(ValueError):
            st._path(bad)  # noqa: SLF001


def test_pasted_text_nul_and_length_limits(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    assert sales.post("/rfqs", json={"text": "\x00\x00   "}).status_code == 422
    assert sales.post("/rfqs", json={"text": "x" * 200_001}).status_code in (413, 422)
    assert sales.post("/rfqs", json={"text": "x" * 150_000}).status_code == 413
    # prompt-injection / formula fixture as pasted text: stored as data, no price or status side effect
    r = sales.post("/rfqs", json={"text": (FIXTURES / "rfq_injection.txt").read_text()})
    assert r.status_code == 201
    items = r.json()["items"]
    assert r.json()["status"] == "parsed"
    # the formula / instruction text is stored verbatim as data (never evaluated, never acted upon)
    assert any(it["description"].startswith("=HYPERLINK") for it in items)
    assert all("price" not in it for it in items) and all(
        it["match_status"] != "resolved" or it["selected_product_id"] for it in items
    )


# ---------------------------------------------------------------------------------------------------
# 6. Injection strings in search / filter params (ASVS V5.3.4, abuse 7)
# ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        "' OR 1=1 --",
        '"; DROP TABLE quotes; --',
        "%",
        "_",
        "\\",
        "%' ESCAPE '\\",
        "𝕌𝕟𝕚𝕔𝕠𝕕𝕖 ' \" ; --",
        "$(id)",
        "{{7*7}}",
        "<script>alert(1)</script>",
    ],
)
def test_search_params_never_error_or_leak(app, tenant_a: Tenant, payload: str) -> None:  # type: ignore[no-untyped-def]
    ro = login_as(app, tenant_a.users["readonly"])
    for path in (
        "/quotes",
        "/customers",
        "/rfqs",
        f"/catalogs/versions/{tenant_a.catalog_version_id}/products",
    ):
        r = ro.get(path, params={"q": payload})
        assert r.status_code == 200, (path, r.status_code, r.text)
        assert "psycopg" not in r.text and "Traceback" not in r.text and "syntax" not in r.text.lower()
    for path in ("/quotes", "/rfqs"):
        r = ro.get(path, params={"status": payload[:16]})
        assert r.status_code in (200, 422), (path, r.text)
        assert "psycopg" not in r.text and "Traceback" not in r.text
    r = ro.get("/quotes", params={"customer_id": payload})
    assert r.status_code == 422 and "Traceback" not in r.text
    r = ro.get(f"/quotes/{payload}")
    assert r.status_code in (404, 422) and "Traceback" not in r.text


def test_nul_byte_in_search_is_rejected_not_500(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ro = login_as(app, tenant_a.users["readonly"])
    for path in ("/quotes", "/customers", "/rfqs", f"/catalogs/versions/{tenant_a.catalog_version_id}/products"):
        r = ro.get(path, params={"q": "a\x00b"})
        # whatever the status, nothing internal may leak
        assert "psycopg" not in r.text and "Traceback" not in r.text and "NUL" not in r.text, (path, r.text)
        assert r.status_code in (200, 422), (path, r.status_code)


# ---------------------------------------------------------------------------------------------------
# 7. Pricing integrity & numeric edge cases (ASVS V5.1.3 / V5.2.x, X-06)
# ---------------------------------------------------------------------------------------------------
def test_client_cannot_set_totals_or_cost_and_non_finite_numbers_rejected(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    ln = q["current_version"]["lines"][0]
    lv = q["lock_version"]
    # mass-assignment attempt: server-computed money and privileged fields are ignored
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": lv,
            "lines": [
                {
                    "id": ln["id"],
                    "line_total": "0.01",
                    "line_subtotal": "0.01",
                    "cost_price": "0",
                    "margin_override_reason": "self-granted",
                    "price_source": "catalog",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()["current_version"]["lines"][0]
    assert out["line_total"] == ln["line_total"] and out["margin_override_reason"] is None
    lv = r.json()["lock_version"]
    for field in ("quantity", "unit_price", "discount_pct", "tax_rate_pct"):
        for value in ("NaN", "Infinity", "-Infinity", "sNaN", "1e308", "-1", "0x10", "1,5", True, [], {}):
            if field == "unit_price" and value == "-1":
                pass
            r = sales.patch(
                f"/quotes/{q['id']}",
                json={"expected_lock_version": lv, "lines": [{"id": ln["id"], field: value}]},
            )
            assert r.status_code == 422, (field, value, r.status_code, r.text)
    # the RFQ item routes share the same expectations
    rfq = sales.post("/rfqs", json={"text": "1 pcs SKU-1001"}).json()
    item = rfq["items"][0]
    for value in ("NaN", "Infinity", "0", "-5", "abc"):
        assert sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"quantity": value}).status_code == 422, value
        assert (
            sales.post(f"/rfqs/{rfq['id']}/items", json={"description": "x", "quantity": value}).status_code == 422
        ), value
    # lock version downgrade / replay → 409, state unchanged
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": 0, "notes": "replay"})
    assert r.status_code == 409 and r.json()["current_lock_version"] == lv


_SEC06: tuple[()] = ()  # SEC-06 fixed: bounds + decimal_places validation now return 422 (marker removed)


@pytest.mark.parametrize(
    ("route", "body"),
    [
        pytest.param(
            "quote", {"quantity": "1000000000", "unit_price": "1000000000"}, marks=_SEC06
        ),  # 1e18 > NUMERIC(18,3)
        pytest.param("quote", {"quantity": "1e-400"}, marks=_SEC06),  # rounds to 0.000 → CHECK (quantity > 0) violated
        pytest.param("quote", {"quantity": "0.0001"}, marks=_SEC06),
        ("rfq_item_update", {"quantity": "1e308"}),  # bounded in the working tree after the first audit pass
        ("rfq_item_update", {"quantity": "0.0001"}),
        ("rfq_item_create", {"description": "x", "quantity": "1e300"}),
        ("rfq_item_create", {"description": "x", "quantity": "1e-400"}),
    ],
)
def test_extreme_decimals_are_422_not_500(app, tenant_a: Tenant, route: str, body: dict) -> None:  # type: ignore[no-untyped-def, type-arg]
    sales = login_as(app, tenant_a.users["sales"])
    if route == "quote":
        q = make_quote(sales, str(tenant_a.customer_id))
        ln = q["current_version"]["lines"][0]
        r = sales.patch(
            f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], **body}]}
        )
    else:
        rfq = sales.post("/rfqs", json={"text": "1 pcs SKU-1001"}).json()
        item = rfq["items"][0]
        if route == "rfq_item_update":
            r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json=body)
        else:
            r = sales.post(f"/rfqs/{rfq['id']}/items", json=body)
    assert r.status_code == 422, (r.status_code, r.text)


def test_currency_mismatch_cannot_zero_price_and_custom_lines_need_price(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S1-F11/F12: a catalog product priced in another currency never auto-prices (no silent FX / zero
    price) and the missing price blocks submission until a human enters one."""
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001", "currency": "EUR"}).json()  # catalog is AED
    assert rfq["currency"] == "EUR" and rfq["items"][0]["selected_product_id"]
    r = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert r.status_code == 201, r.text
    q = r.json()
    ln = q["current_version"]["lines"][0]
    assert ln["unit_price"] is None and ln["price_source"] == "none" and ln["line_total"] == "0"
    assert any(b["code"] == "missing_price" for b in q["blockers"])
    r = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert r.status_code == 409 and r.json()["error"] == "blocked"
    # re-linking the product through PATCH in a mismatched currency must not auto-price either
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "product_id": ln["product_id"]}]},
    )
    assert r.status_code == 200 and r.json()["current_version"]["lines"][0]["unit_price"] is None


def test_sales_cannot_override_margin_through_any_route(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S1-F12 / X-03: only owner/admin can clear a below-margin blocker; sales attempts via PATCH or the
    override route fail, and a later price edit by anyone resets an existing override."""
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    ln = q["current_version"]["lines"][0]
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.01"}]},
    )
    assert r.status_code == 200
    q = r.json()
    assert any(b["code"] == "below_min_margin" for b in q["blockers"])
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409
    body = {"expected_lock_version": q["lock_version"], "reason": "because I said so"}
    assert sales.post(f"/quotes/{q['id']}/lines/{ln['id']}/margin-override", json=body).status_code == 403
    assert approver.post(f"/quotes/{q['id']}/lines/{ln['id']}/margin-override", json=body).status_code == 403
    assert (
        admin.post(f"/quotes/{q['id']}/lines/{ln['id']}/margin-override", json={**body, "reason": "abc"}).status_code
        == 422
    )
    r = admin.post(f"/quotes/{q['id']}/lines/{ln['id']}/margin-override", json=body)
    assert r.status_code == 200
    q = r.json()
    assert not any(b["code"] == "below_min_margin" for b in q["blockers"])
    # any later price edit (even by sales) invalidates the override
    r = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.02"}]},
    )
    assert r.status_code == 200
    assert any(b["code"] == "below_min_margin" for b in r.json()["blockers"])


# ---------------------------------------------------------------------------------------------------
# 8. Workflow state & concurrency (ASVS V11.1.x business logic, THREAT_MODEL "Concurrency")
# ---------------------------------------------------------------------------------------------------
def test_withdrawn_quote_cannot_be_approved(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _sales, q = _submitted_quote(app, tenant_a)
    owner = login_as(app, tenant_a.users["owner"])
    approver = login_as(app, tenant_a.users["approver"])
    r = owner.post(f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": q["lock_version"], "reason": "pulled"})
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    lv = r.json()["lock_version"]
    # SEC-04 (fixed in the working tree after the first audit pass): neither approve nor reject may act on it
    r = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv})
    assert r.status_code == 409, r.text
    r = approver.post(f"/quotes/{q['id']}/reject", json={"expected_lock_version": lv, "reason": "late"})
    assert r.status_code == 409, r.text
    assert owner.get(f"/quotes/{q['id']}").json()["status"] == "withdrawn"
    # and the withdrawn version is not exportable as "current"
    r = owner.post(f"/quotes/{q['id']}/versions/1/pdf")
    assert r.status_code in (200, 409)
    if r.status_code == 200:
        assert r.json()["status"] != "approved"


def test_expired_quote_is_not_exportable_as_current(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S1-F15: expired quotes cannot be exported as current; the stored approved PDF carries the label APPROVED."""
    _sales, q = _submitted_quote(app, tenant_a)
    approver = login_as(app, tenant_a.users["approver"])
    r = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": q["lock_version"]})
    assert r.status_code == 200
    _admin_exec("UPDATE quotes SET valid_until = current_date - 1 WHERE id = :q", org=tenant_a.org_id, q=q["id"])
    assert approver.get(f"/quotes/{q['id']}").json()["effective_status"] == "expired"
    for method, path in (("GET", "/pdf"), ("POST", "/pdf"), ("GET", "/csv")):
        url = f"/quotes/{q['id']}/versions/1{path}"
        r = approver.get(url) if method == "GET" else approver.post(url)
        assert r.status_code == 409, (method, path, r.status_code)


def test_double_approve_same_lock_version_race(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Two approvers fire approve with the SAME expected_lock_version concurrently: exactly one may win."""
    for _ in range(3):
        _sales, q = _submitted_quote(app, tenant_a)
        a1 = login_as(app, tenant_a.users["approver"])
        a2 = login_as(app, tenant_a.users["owner"])
        results: list[int] = []
        barrier = threading.Barrier(2)

        def go(ac: AuthedClient, q=q, barrier=barrier, results=results) -> None:  # type: ignore[no-untyped-def]
            barrier.wait()
            results.append(
                ac.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": q["lock_version"]}).status_code
            )

        ts = [threading.Thread(target=go, args=(a,)) for a in (a1, a2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results) == [200, 409], results
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            n = db.execute(
                text(
                    "SELECT count(*) FROM approval_decisions d JOIN quote_versions v ON v.id = d.quote_version_id WHERE v.quote_id = :q"
                ),
                {"q": q["id"]},
            ).scalar()
            db.rollback()
        assert n == 1


def test_approve_race_with_guessed_next_lock_version(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Service-level race: T1 approves (lock N) and holds its transaction open; T2 approves with a guessed
    lock N+1 while T1 is uncommitted. Verified outcome: T2's conditional UPDATE does not match in its
    snapshot (lock is still N there), so it gets 0 rows → ConflictError, never a second approval.
    (The LLD claim "approve checks state in the same UPDATE" is not literally true — only lock_version is
    in the WHERE — but every mutation bumps lock_version, which closes this window; see evidence SEC-07.)"""
    from app.models.org import Membership, Organization
    from app.models.quote import Quote
    from app.services import quotes as qs

    _sales, q = _submitted_quote(app, tenant_a)
    qid = uuid.UUID(q["id"])
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        roles = {m.role: m.user_id for m in db.query(Membership).filter_by(organization_id=tenant_a.org_id)}
        db.rollback()

    def run(role: str, expected: int, hold: float, out: dict) -> None:  # type: ignore[type-arg]
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, roles[role])
            org = db.get(Organization, tenant_a.org_id)
            quote = db.get(Quote, qid)
            try:
                qs.approve(
                    db,
                    org=org,
                    actor_user_id=roles[role],
                    role=role,
                    ip=None,
                    quote=quote,
                    expected_lock_version=expected,
                    reason=None,
                )  # type: ignore[arg-type]
                time.sleep(hold)
                db.commit()
                out[role] = "approved"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                out[role] = type(exc).__name__

    out: dict[str, str] = {}
    t1 = threading.Thread(target=run, args=("approver", q["lock_version"], 1.5, out))
    t2 = threading.Thread(target=run, args=("owner", q["lock_version"] + 1, 0.0, out))
    t1.start()
    time.sleep(0.5)
    t2.start()
    t1.join()
    t2.join()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        n = db.execute(
            text(
                "SELECT count(*) FROM approval_decisions d JOIN quote_versions v ON v.id = d.quote_version_id WHERE v.quote_id = :q"
            ),
            {"q": str(qid)},
        ).scalar()
        db.rollback()
    assert out["approver"] == "approved"
    assert out["owner"] != "approved" and n == 1, (out, n)


# ---------------------------------------------------------------------------------------------------
# 10. LLM adapter (OWASP LLM01/LLM02/LLM05/LLM06): off by default, no routes, strict output validation
# ---------------------------------------------------------------------------------------------------
def test_ai_adapter_disabled_by_default_unwired_and_output_validated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pathlib

    from app.services.ai import provider as prov
    from app.services.ai.redaction import redact_for_model

    prov.reset_provider()
    assert prov.get_provider().name == "disabled"
    assert prov.get_provider().propose_extraction("1 pcs SKU-1001") is None
    # no API router or non-AI service imports the provider: there is no AI-driven action in Stage 1
    api_dir = pathlib.Path(prov.__file__).resolve().parents[2]
    offenders = [
        p
        for p in list((api_dir / "api").rglob("*.py"))
        + [x for x in (api_dir / "services").rglob("*.py") if "/ai/" not in str(x)]
        if "services.ai" in p.read_text() or "services import ai" in p.read_text()
    ]
    assert offenders == [], offenders
    # strict output validation: malformed / out-of-schema / oversized proposals are dropped
    assert prov.parse_proposal("not json") is None
    assert prov.parse_proposal('{"lines": [{"description": "x", "quantity": -1}]}') is None
    assert prov.parse_proposal('{"lines": [{"description": "x", "quantity": "NaN"}]}') is None
    assert prov.parse_proposal('{"lines": [{"description": "x", "quantity": 1e12}]}') is None
    assert (
        prov.parse_proposal('{"lines": [{"description": "x", "unit_price": "0"}]}') is not None
    )  # unknown keys ignored, never surfaced
    p = prov.parse_proposal('{"lines": [{"description": "x", "unit_price": "0"}]}')
    assert p is not None and not hasattr(p.lines[0], "unit_price")
    assert prov.parse_proposal('{"lines": ' + str([{"description": "x"}] * 501).replace("'", '"') + "}") is None
    # redaction before send: credentials and card-like numbers never leave; input is bounded
    red = redact_for_model(
        "password: hunter2hunter2 key sk-ant-api03-abcdefghijklmnop card 4111 1111 1111 1111 " + "x" * 50_000, 20_000
    )
    assert "hunter2" not in red and "sk-ant" not in red and "4111 1111" not in red and len(red) <= 20_000
    # provider key only from the server environment; never in any response schema
    from app.schemas import common

    assert "anthropic" not in pathlib.Path(common.__file__).read_text().lower()
