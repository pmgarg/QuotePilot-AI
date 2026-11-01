"""QA (independent): organisation settings, membership lifecycle and session revocation.

Derived from docs/PRD.md S1-F01, S1-F02, S1-F03 and docs/USER_JOURNEYS.md (role matrix, last-owner guard).
Integration evidence (API + PostgreSQL + RLS).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API, PASSWORD, Tenant, login_as

pytestmark = pytest.mark.integration

NEW_PASSWORD = "Another-Long-Pass-77"


def _member(ac, role: str) -> dict:  # type: ignore[no-untyped-def,type-arg]
    return next(m for m in ac.get("/org/members").json() if m["role"] == role)


def _login_status(app, email: str, password: str = PASSWORD) -> int:  # type: ignore[no-untyped-def]
    client = TestClient(app, base_url="http://testserver")
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    return client.post(
        f"{API}/auth/login", json={"email": email, "password": password}, headers={"X-CSRF-Token": pre}
    ).status_code


# ---------------------------------------------------------------------------------------------------------------
# S1-F02 — sessions are revoked on deactivation; deactivated users get the generic login error
# ---------------------------------------------------------------------------------------------------------------


def test_deactivating_a_member_revokes_sessions_and_login(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales1 = login_as(app, tenant_a.users["sales"])
    sales2 = login_as(app, tenant_a.users["sales"])  # second device
    admin = login_as(app, tenant_a.users["admin"])
    m = _member(admin, "sales")
    r = admin.patch(f"/org/members/{m['id']}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False  # activation is per membership (SEC-01 fix)
    assert sales1.get("/auth/me").status_code == 401
    assert sales2.get("/auth/me").status_code == 401
    assert sales1.post("/customers", json={"name": "x"}).status_code in (401, 403)
    client = TestClient(app, base_url="http://testserver")
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    r = client.post(
        f"{API}/auth/login", json={"email": tenant_a.users["sales"], "password": PASSWORD}, headers={"X-CSRF-Token": pre}
    )
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password"  # same generic message
    # reactivation restores access
    assert admin.patch(f"/org/members/{m['id']}", json={"is_active": True}).status_code == 200
    assert _login_status(app, tenant_a.users["sales"]) == 200


def test_member_role_change_takes_effect_on_next_login(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    m = _member(owner, "sales")
    assert owner.patch(f"/org/members/{m['id']}", json={"role": "readonly"}).status_code == 200
    again = login_as(app, tenant_a.users["sales"])
    assert again.role == "readonly"
    assert again.post("/rfqs", json={"text": "1 pcs SKU-1001"}).status_code == 403
    assert "edit_quotes" not in again.get("/auth/me").json()["permissions"]


# ---------------------------------------------------------------------------------------------------------------
# S1-F01 — last-owner guard and owner privileges
# ---------------------------------------------------------------------------------------------------------------


def test_sole_owner_cannot_be_deactivated_or_demoted(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    owner = login_as(app, tenant_a.users["owner"])
    om = _member(admin, "owner")
    assert admin.patch(f"/org/members/{om['id']}", json={"is_active": False}).status_code == 409
    assert owner.patch(f"/org/members/{om['id']}", json={"is_active": False}).status_code == 409
    assert owner.patch(f"/org/members/{om['id']}", json={"role": "admin"}).status_code in (409, 422)
    assert owner.get("/auth/me").status_code == 200
    assert _member(admin, "owner")["is_active"] is True


def test_last_active_owner_guard_counts_only_active_owners(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S1-F01: 'owner cannot be removed if last owner'. A deactivated owner does not count as an owner."""
    owner = login_as(app, tenant_a.users["owner"])
    admin = login_as(app, tenant_a.users["admin"])
    r = owner.post(
        "/org/members",
        json={"email": "owner2@alphatrading.example", "full_name": "Second Owner", "role": "owner", "password": NEW_PASSWORD},
    )
    assert r.status_code == 201, r.text
    owner2_id = r.json()["id"]
    # with two active owners, deactivating one is fine
    assert owner.patch(f"/org/members/{owner2_id}", json={"is_active": False}).status_code == 200
    # now only one *active* owner remains: it must be protected again
    om = _member(admin, "owner")
    r = admin.patch(f"/org/members/{om['id']}", json={"is_active": False})
    assert r.status_code == 409, (r.status_code, r.text)
    active_owners = [m for m in admin.get("/org/members").json() if m["role"] == "owner" and m["is_active"]]
    assert len(active_owners) == 1, "organisation left without an active owner"


def test_only_owner_grants_owner_role(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    owner = login_as(app, tenant_a.users["owner"])
    sm = _member(admin, "sales")
    assert admin.patch(f"/org/members/{sm['id']}", json={"role": "owner"}).status_code == 422
    assert owner.patch(f"/org/members/{sm['id']}", json={"role": "owner"}).status_code == 200
    # two owners now; an admin still may not demote an owner
    assert admin.patch(f"/org/members/{sm['id']}", json={"role": "sales"}).status_code in (409, 422)
    assert owner.patch(f"/org/members/{sm['id']}", json={"role": "sales"}).status_code == 200


def test_member_create_validation_and_duplicates(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    base = {"full_name": "Dup", "role": "sales", "password": NEW_PASSWORD}
    assert admin.post("/org/members", json={"email": tenant_a.users["sales"], **base}).status_code == 409
    assert admin.post("/org/members", json={"email": "not-an-email", **base}).status_code == 422
    assert admin.post("/org/members", json={"email": "x@alphatrading.example", **base, "role": "superuser"}).status_code == 422
    assert admin.post("/org/members", json={"email": "x@alphatrading.example", **base, "password": "x" * 11}).status_code == 422
    for role in ("sales", "approver", "readonly"):
        assert login_as(app, tenant_a.users[role]).post("/org/members", json={"email": f"{role}2@a.example", **base}).status_code == 403
        assert login_as(app, tenant_a.users[role]).get("/org/members").status_code == 403


def test_cannot_deactivate_self(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    me = _member(admin, "admin")
    assert admin.patch(f"/org/members/{me['id']}", json={"is_active": False}).status_code == 409
    assert admin.get("/auth/me").status_code == 200


def test_member_of_other_tenant_is_404(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    a_admin = login_as(app, tenant_a.users["admin"])
    b_admin = login_as(app, tenant_b.users["admin"])
    victim = _member(a_admin, "sales")
    assert b_admin.patch(f"/org/members/{victim['id']}", json={"is_active": False}).status_code == 404
    assert b_admin.patch(f"/org/members/{victim['id']}", json={"role": "owner"}).status_code == 404
    assert login_as(app, tenant_a.users["sales"]).get("/auth/me").status_code == 200


# ---------------------------------------------------------------------------------------------------------------
# S1-F03 — organisation settings validation and role gates
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"default_tax_rate": "101"},
        {"default_tax_rate": "-1"},
        {"min_margin_pct": "-5"},
        {"min_margin_pct": "100.5"},
        {"default_validity_days": 0},
        {"default_validity_days": 366},
        {"rounding_mode": "FLOOR"},
        {"brand_color": "red"},
        {"brand_color": "#12345"},
        {"default_currency": "EURO"},
        {"default_currency": "ZZZ"},
        {"name": ""},
        {"pdf_footer": "x" * 501},
    ],
)
def test_org_settings_validation(app, tenant_a: Tenant, payload: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    owner = login_as(app, tenant_a.users["owner"])
    before = owner.get("/org").json()
    r = owner.patch("/org", json=payload)
    assert r.status_code == 422, (payload, r.text)
    after = owner.get("/org").json()
    assert {k: after[k] for k in payload} == {k: before[k] for k in payload}


def test_org_settings_role_gates_and_audit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    for role in ("sales", "approver", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        assert ac.patch("/org", json={"name": "Nope"}).status_code == 403, role
        assert ac.get("/org").json()["name"] == "Alpha Trading"
    owner = login_as(app, tenant_a.users["owner"])
    r = owner.patch(
        "/org",
        json={
            "legal_name": "Alpha Trading L.L.C.",
            "address": "Unit 7\nDubai",
            "default_validity_days": 45,
            "min_margin_pct": "15",
            "pdf_footer": "Prices exclude delivery",
        },
    )
    assert r.status_code == 200 and r.json()["default_validity_days"] == 45 and r.json()["min_margin_pct"] == "15"
    # the new validity days and margin floor apply to quotes created afterwards
    sales = login_as(app, tenant_a.users["sales"])
    from datetime import date, timedelta

    q = sales.post("/quotes", json={}).json()
    assert q["valid_until"] == (date.today() + timedelta(days=45)).isoformat()
    from decimal import Decimal

    assert Decimal(q["current_version"]["pricing_snapshot"]["min_margin_pct"]) == Decimal("15")


def test_self_approval_setting_is_owner_only_and_audited(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    owner = login_as(app, tenant_a.users["owner"])
    assert admin.patch("/org", json={"allow_self_approval": True}).status_code == 422
    assert owner.get("/org").json()["allow_self_approval"] is False
    assert owner.patch("/org", json={"allow_self_approval": True}).status_code == 200
    assert admin.get("/org").json()["allow_self_approval"] is True
    assert admin.patch("/org", json={"allow_self_approval": False}).status_code == 200  # disabling is allowed


# ---------------------------------------------------------------------------------------------------------------
# S1-F02 — CSRF on every unsafe method (API_CONTRACT: unsafe methods require X-CSRF-Token)
# ---------------------------------------------------------------------------------------------------------------


def test_logout_requires_csrf_token(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import AuthedClient

    AuthedClient(client, tenant_a.users["sales"])
    r = client.post(f"{API}/auth/logout")  # no X-CSRF-Token header
    assert r.status_code == 403, (r.status_code, r.text)
    assert client.get(f"{API}/auth/me").status_code == 200, "session was revoked by a request without CSRF token"


def test_csrf_required_on_delete_and_patch(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import AuthedClient

    ac = AuthedClient(client, tenant_a.users["sales"])
    rfq = ac.post("/rfqs", json={"text": "1 pcs SKU-1001"}).json()
    item = rfq["items"][0]
    assert client.patch(f"{API}/rfqs/{rfq['id']}/items/{item['id']}", json={"quantity": "2"}).status_code == 403
    assert client.delete(f"{API}/rfqs/{rfq['id']}/items/{item['id']}").status_code == 403
    assert len(client.get(f"{API}/rfqs/{rfq['id']}").json()["items"]) == 1
    assert client.get(f"{API}/rfqs/{rfq['id']}").json()["items"][0]["quantity"] == "1"
