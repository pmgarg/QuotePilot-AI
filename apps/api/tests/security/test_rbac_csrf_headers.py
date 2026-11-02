import pytest

from tests.conftest import API, Tenant, login_as
from tests.integration.test_quotes import make_quote

pytestmark = [pytest.mark.integration, pytest.mark.security]


def test_vertical_privilege_matrix(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ro = login_as(app, tenant_a.users["readonly"])
    sales = login_as(app, tenant_a.users["sales"])
    appr = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    lv = q["lock_version"]
    body = {"expected_lock_version": lv, "reason": "x"}
    assert ro.post("/quotes", json={}).status_code == 403
    assert ro.patch(f"/quotes/{q['id']}", json={"expected_lock_version": lv, "notes": "x"}).status_code == 403
    assert ro.post(f"/quotes/{q['id']}/submit", json=body).status_code == 403
    assert ro.post(f"/quotes/{q['id']}/approve", json=body).status_code == 403
    assert (
        ro.post(
            "/org/members",
            json={"email": "a@b.example", "full_name": "x", "role": "owner", "password": "Long-Enough-Pass-1"},
        ).status_code
        == 403
    )
    assert ro.get(f"/quotes/{q['id']}").status_code == 200  # read allowed
    assert ro.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 200  # export allowed, costs hidden
    assert "cost_price" not in ro.get(f"/quotes/{q['id']}/versions/1/csv").text
    assert (
        sales.post(
            f"/quotes/{q['id']}/lines/{q['current_version']['lines'][0]['id']}/margin-override", json=body
        ).status_code
        == 403
    )
    assert appr.patch("/org", json={"name": "x"}).status_code == 403
    assert appr.post("/catalogs/versions/" + str(tenant_a.catalog_version_id) + "/activate").status_code == 403


def test_csrf_required_on_state_changes(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import AuthedClient

    ac = AuthedClient(client, tenant_a.users["sales"])
    # missing header
    r = client.post(f"{API}/customers", json={"name": "x"})
    assert r.status_code == 403 and r.json()["action"] == "csrf"
    # wrong header
    r = client.post(f"{API}/customers", json={"name": "x"}, headers={"X-CSRF-Token": "0" * 64})
    assert r.status_code == 403
    # header without matching cookie
    saved = client.cookies.get("qp_csrf")
    client.cookies.delete("qp_csrf")
    r = client.post(f"{API}/customers", json={"name": "x"}, headers={"X-CSRF-Token": ac.csrf})
    assert r.status_code == 403
    client.cookies.set("qp_csrf", saved)
    assert ac.post("/customers", json={"name": "ok"}).status_code == 201


def test_security_headers_and_cookie_flags(app, client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"{API}/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Request-ID"]
    pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
    from tests.conftest import PASSWORD

    r = client.post(
        f"{API}/auth/login",
        json={"email": tenant_a.users["sales"], "password": PASSWORD},
        headers={"X-CSRF-Token": pre},
    )
    set_cookie = " ".join(v for k, v in r.headers.multi_items() if k.lower() == "set-cookie")
    assert (
        "qp_session=" in set_cookie and "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie.replace("Lax", "lax")
    )
    assert "Path=/" in set_cookie


def test_unknown_errors_do_not_leak_internals(app, client) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"{API}/quotes/not-a-uuid")
    assert r.status_code in (401, 422)
    r = client.get(f"{API}/does-not-exist")
    assert r.status_code == 404 and r.json()["error"] == "http_error"


def test_request_id_header_sanitised(app, client) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"{API}/health", headers={"X-Request-ID": "<script>alert(1)</script>"})
    assert "<" not in r.headers["X-Request-ID"]
    r = client.get(f"{API}/health", headers={"X-Request-ID": "trace-abcdef123456"})
    assert r.headers["X-Request-ID"] == "trace-abcdef123456"
