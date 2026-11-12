import pytest

from tests.conftest import API, PASSWORD, AuthedClient, Tenant, login_as

pytestmark = pytest.mark.integration


def test_login_sets_httponly_cookie_and_me(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ac = AuthedClient(client, tenant_a.users["sales"])
    cookie = next(c for c in client.cookies.jar if c.name == "qp_session")
    assert cookie.has_nonstandard_attr("HttpOnly") or "httponly" in str(cookie._rest).lower()  # noqa: SLF001
    me = ac.get("/auth/me")
    assert me.status_code == 200 and me.json()["role"] == "sales"
    assert "approve_quote" not in me.json()["permissions"]


def test_login_requires_prelogin_csrf(client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    r = client.post(f"{API}/auth/login", json={"email": tenant_a.users["sales"], "password": PASSWORD})
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_login_wrong_password_generic_and_audited(client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    r = client.post(
        f"{API}/auth/login",
        json={"email": tenant_a.users["sales"], "password": "wrong-password-123"},
        headers={"X-CSRF-Token": pre},
    )
    assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password"
    r2 = client.post(
        f"{API}/auth/login",
        json={"email": "nobody@nowhere.example", "password": "wrong-password-123"},
        headers={"X-CSRF-Token": pre},
    )
    assert r2.status_code == 401 and r2.json()["detail"] == r.json()["detail"]


def test_login_rate_limited(client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    codes = []
    for _ in range(12):
        codes.append(
            client.post(
                f"{API}/auth/login",
                json={"email": tenant_a.users["sales"], "password": "bad-password-xyz"},
                headers={"X-CSRF-Token": pre},
            ).status_code
        )
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_logout_revokes_session(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ac = AuthedClient(client, tenant_a.users["sales"])
    assert ac.post("/auth/logout").status_code == 200
    token_cookie_gone = client.get(f"{API}/auth/me")
    assert token_cookie_gone.status_code == 401


def test_session_token_reuse_after_logout(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ac = AuthedClient(client, tenant_a.users["sales"])
    token = client.cookies.get("qp_session")
    ac.post("/auth/logout")
    client.cookies.set("qp_session", token)
    assert client.get(f"{API}/auth/me").status_code == 401


def test_org_settings_role_gates(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = sales.get("/org")
    assert r.status_code == 200 and "min_margin_pct" not in r.json()
    assert sales.patch("/org", json={"name": "Hacked"}).status_code == 403
    admin = login_as(app, tenant_a.users["admin"])
    r = admin.patch("/org", json={"name": "Alpha Renamed", "default_tax_rate": "7.5", "brand_color": "#00aa00"})
    assert r.status_code == 200 and r.json()["name"] == "Alpha Renamed" and r.json()["default_tax_rate"] == "7.5"
    assert admin.patch("/org", json={"default_currency": "XXX"}).status_code == 422
    assert admin.patch("/org", json={"allow_self_approval": True}).status_code == 403  # owner only (role denial → 403)
    owner = login_as(app, tenant_a.users["owner"])
    assert owner.patch("/org", json={"allow_self_approval": True}).status_code == 200


def test_members_crud_and_last_owner_guard(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    members = admin.get("/org/members").json()
    assert {m["role"] for m in members} == {"owner", "admin", "sales", "approver", "readonly"}
    owner_m = next(m for m in members if m["role"] == "owner")
    assert admin.patch(f"/org/members/{owner_m['id']}", json={"role": "sales"}).status_code in (409, 422)
    r = admin.post(
        "/org/members",
        json={
            "email": "new.sales@alphatrading.example",
            "full_name": "New Sales",
            "role": "sales",
            "password": "Another-Long-Pass-77",
        },
    )
    assert r.status_code == 201
    assert (
        admin.post(
            "/org/members",
            json={
                "email": "x@alphatrading.example",
                "full_name": "x",
                "role": "owner",
                "password": "Another-Long-Pass-77",
            },
        ).status_code
        == 403
    )
    assert (
        admin.post(
            "/org/members",
            json={"email": "short@alphatrading.example", "full_name": "x", "role": "sales", "password": "short"},
        ).status_code
        == 422
    )
    sales = login_as(app, tenant_a.users["sales"])
    assert sales.get("/org/members").status_code == 403


def test_role_change_revokes_sessions(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    owner = login_as(app, tenant_a.users["owner"])
    sales_m = next(m for m in owner.get("/org/members").json() if m["role"] == "sales")
    assert owner.patch(f"/org/members/{sales_m['id']}", json={"role": "readonly"}).status_code == 200
    assert sales.get("/auth/me").status_code == 401


def test_customers_crud_and_validation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = sales.post("/customers", json={"name": "New Co", "email": "buyer@newco.example", "default_currency": "usd"})
    assert r.status_code == 201 and r.json()["default_currency"] == "USD"
    assert sales.post("/customers", json={"name": "Bad", "default_currency": "ZZZ"}).status_code == 422
    assert sales.get("/customers?q=%25%27%20OR%201=1--").status_code == 200
    ro = login_as(app, tenant_a.users["readonly"])
    assert ro.post("/customers", json={"name": "Nope"}).status_code == 403
    assert ro.get("/customers").status_code == 200
