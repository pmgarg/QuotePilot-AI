"""Regression tests for independent release review 1 findings (docs/evidence/release-review-1-stage1-2026-11-01.md)."""

import hashlib
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.db.session import get_sessionmaker, set_auth_scope, set_tenant
from tests.conftest import API, PASSWORD, Tenant, login_as, make_tenant
from tests.integration.test_quotes import make_quote

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _approve(app, tenant: Tenant, text_: str | None = None):  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant.users["sales"])
    approver = login_as(app, tenant.users["approver"])
    q = make_quote(sales, str(tenant.customer_id), text_)
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    assert a.status_code == 200, a.text
    return sales, approver, a.json()


def test_h1_approved_document_immutable_after_new_version_and_validity_edit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = _approve(app, tenant_a)
    v1 = next(v for v in q["versions"] if v["version_no"] == 1)
    assert v1["pdf_sha256"], "approved version must be rendered and stored at approval time"
    first = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert first.status_code == 200
    assert hashlib.sha256(first.content).hexdigest() == v1["pdf_sha256"]
    # sales opens a new draft version and pushes validity into the far future; org renames itself
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    assert (
        sales.patch(
            f"/quotes/{q['id']}", json={"expected_lock_version": nv["lock_version"], "valid_until": "2099-12-31"}
        ).status_code
        == 200
    )
    owner = login_as(app, tenant_a.users["owner"])
    assert owner.patch("/org", json={"name": "Renamed After Approval"}).status_code == 200
    again = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert again.status_code == 200
    assert again.content == first.content  # byte-identical stored artifact
    assert b"2099-12-31" not in again.content and b"Renamed After Approval" not in again.content
    # the CSV of v1 is unchanged lines; and the stored sha is untouched
    hist = sales.get(f"/quotes/{q['id']}").json()
    assert next(v for v in hist["versions"] if v["version_no"] == 1)["pdf_sha256"] == v1["pdf_sha256"]


def test_h1_expiry_judged_on_frozen_validity(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = _approve(app, tenant_a)
    with get_sessionmaker()() as db:  # simulate time passing: freeze the snapshot in the past (owner-free path)
        set_tenant(db, tenant_a.org_id, None)
        db.execute(
            text(
                "UPDATE quote_versions SET document_snapshot = jsonb_set(document_snapshot, '{valid_until}', to_jsonb(CAST(:d AS text))) "
                "WHERE quote_id = :q AND version_no = 1"
            ),
            {"d": (date.today() - timedelta(days=1)).isoformat(), "q": q["id"]},
        )
        db.commit()
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    assert sales.get(f"/quotes/{q['id']}/versions/1/csv").status_code == 409
    # reviving the header validity does not revive the approved document
    nv = sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": q["lock_version"]}).json()
    sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": nv["lock_version"], "valid_until": "2099-12-31"})
    assert sales.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409


def test_m1_login_limiter_ignores_email_whitespace_and_case(client, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    email = tenant_a.users["sales"]
    variants = [email, f" {email}", f"{email} ", f"\t{email}\t", email.upper(), f" {email.upper()} "]
    codes = []
    for i in range(12):
        pre = client.get(f"{API}/auth/csrf").json()["csrf_token"]
        codes.append(
            client.post(
                f"{API}/auth/login",
                json={"email": variants[i % len(variants)], "password": "wrong-pass-xyz"},
                headers={"X-CSRF-Token": pre},
            ).status_code
        )
    assert 429 in codes[:11], codes


def test_m3_blocker_text_is_role_aware(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "discount_pct": "40"}]},
    ).json()
    msgs = " ".join(b["message"] for b in q["blockers"])
    assert "below_min_margin" in {b["code"] for b in q["blockers"]}
    assert "%" not in msgs and "12" not in msgs, msgs
    blocked = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    assert "%" not in " ".join(b["message"] for b in blocked["blockers"])
    priv = approver.get(f"/quotes/{q['id']}").json()
    assert any("%" in b["message"] for b in priv["blockers"])  # approvers may see the figures


def test_m4_manual_price_requires_privileged_confirmation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                {"description": "Hex bolt M8x40 (custom)", "quantity": "10", "unit_price": "0.01", "is_custom": True}
            ],
        },
    ).json()
    codes = {b["code"] for b in q["blockers"]}
    assert "manual_price_unconfirmed" in codes
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 409
    line = q["current_version"]["lines"][0]
    assert (
        sales.post(
            f"/quotes/{q['id']}/lines/{line['id']}/price-override",
            json={"expected_lock_version": q["lock_version"], "reason": "agreed with customer"},
        ).status_code
        == 403
    )
    r = admin.post(
        f"/quotes/{q['id']}/lines/{line['id']}/price-override",
        json={"expected_lock_version": q["lock_version"], "reason": "one-off clearance price agreed by GM"},
    )
    assert r.status_code == 200 and r.json()["blockers"] == []
    q = sales.get(f"/quotes/{q['id']}").json()
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 200
    # typing over a catalog price also needs confirmation
    q2 = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q2["current_version"]["lines"][0]
    q2 = sales.patch(
        f"/quotes/{q2['id']}",
        json={"expected_lock_version": q2["lock_version"], "lines": [{"id": ln["id"], "unit_price": "5.00"}]},
    ).json()
    assert "manual_price_unconfirmed" in {b["code"] for b in q2["blockers"]}


def test_l4_total_overflow_rejected(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    lines = [
        {"description": f"big {i}", "quantity": "10000000", "unit_price": "10000000", "is_custom": True}
        for i in range(3)
    ]
    r = sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "lines": lines})
    assert r.status_code == 422, r.text
    assert "maximum" in r.json()["detail"]


def test_l5_oversized_rfq_and_catalog_are_rejected_not_truncated(
    app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from app.core import config

    class S(config.Settings):
        max_rfq_rows: int = 5
        max_catalog_rows: int = 5

    monkeypatch.setattr("app.services.parsing.sandbox.get_settings", lambda: S())
    monkeypatch.setattr("app.services.rfqs.get_settings", lambda: S())
    monkeypatch.setattr("app.services.catalog_import.get_settings", lambda: S())
    sales = login_as(app, tenant_a.users["sales"])
    csv = "Item,Qty\n" + "".join(f"Widget {i},1\n" for i in range(20))
    rfq = sales.post("/rfqs", files={"file": ("big.csv", csv.encode(), "text/csv")}).json()
    assert rfq["status"] == "failed" and "too_large" in rfq["failure_reason"] and rfq["items"] == []
    admin = login_as(app, tenant_a.users["admin"])
    cat = "SKU,Description,Unit,Currency,List Price\n" + "".join(f"S{i},Thing {i},EA,AED,1\n" for i in range(20))
    r = admin.post(
        "/catalogs/import",
        files={"file": ("c.csv", cat.encode(), "text/csv")},
        data={"mapping": '{"sku":"SKU","description":"Description","list_price":"List Price"}'},
    )
    assert r.status_code == 413


def test_l3_last_owner_guard_is_serialised(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    owner = login_as(app, tenant_a.users["owner"])
    r = owner.post(
        "/org/members",
        json={
            "email": "owner2@alphatrading.example",
            "full_name": "Second Owner",
            "role": "owner",
            "password": PASSWORD,
        },
    )
    assert r.status_code == 201
    owner2_mid = r.json()["id"]
    owner2 = login_as(app, "owner2@alphatrading.example")
    owner1_mid = next(
        m["id"] for m in owner.get("/org/members").json() if m["user"]["email"] == tenant_a.users["owner"]
    )
    import concurrent.futures

    def a():  # type: ignore[no-untyped-def]
        return owner.patch(f"/org/members/{owner2_mid}", json={"is_active": False}).status_code

    def b():  # type: ignore[no-untyped-def]
        return owner2.patch(f"/org/members/{owner1_mid}", json={"is_active": False}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        results = sorted([ex.submit(a).result(), ex.submit(b).result()])
    # exactly one wins; the loser is refused by the guard (409) or already lost its session (401)
    assert results[0] == 200 and results[1] in (401, 409), results
    active = [m for m in owner.get("/org/members").json() if m["role"] == "owner" and m["is_active"]] or [
        m for m in owner2.get("/org/members").json() if m["role"] == "owner" and m["is_active"]
    ]
    assert len(active) == 1


def test_l1_users_and_sessions_rls_scopes(tenant_a: Tenant, tenant_b: Tenant) -> None:
    with get_sessionmaker()() as db:
        # no context at all → nothing
        assert db.execute(text("SELECT count(*) FROM users")).scalar() == 0
        assert db.execute(text("SELECT count(*) FROM sessions")).scalar() == 0
        db.rollback()
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        emails = {e for (e,) in db.execute(text("SELECT email FROM users")).all()}
        assert emails == set(tenant_a.users.values())  # only members of the current org
        # a tenant context may not UPDATE another member's row (password hash) — only the acting user may
        res = db.execute(text("UPDATE users SET full_name = 'pwned' WHERE email = :e"), {"e": tenant_a.users["sales"]})
        assert res.rowcount == 0
        db.rollback()
    with get_sessionmaker()() as db:
        set_auth_scope(db, login_email=tenant_b.users["owner"])
        assert db.execute(text("SELECT count(*) FROM users")).scalar() == 1  # login scope reveals exactly that row
        with pytest.raises(Exception, match="row-level security"):
            db.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, full_name, is_active, created_at, updated_at) VALUES (gen_random_uuid(), 'x@y.example', 'h', 'x', true, now(), now())"
                )
            )
        db.rollback()
    with get_sessionmaker()() as db:
        assert db.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'users'")).scalar() is True
        assert db.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'sessions'")).scalar() is True
        pols = {
            p
            for (p,) in db.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename IN ('users','sessions')")
            ).all()
        }
        assert {
            "users_select",
            "users_insert",
            "users_update",
            "sessions_select",
            "sessions_insert",
            "sessions_update",
        } <= pols


def test_x05_ai_consent_is_owner_only_and_recorded(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    owner = login_as(app, tenant_a.users["owner"])
    assert admin.patch("/org", json={"ai_processing_consent": True}).status_code == 403
    r = owner.patch("/org", json={"ai_processing_consent": True})
    assert r.status_code == 200 and r.json()["ai_processing_consented_at"]
    from app.services.ai.provider import ai_enabled_for

    class OrgStub:
        ai_processing_consented_at = "2026-01-01T00:00:00Z"

    assert ai_enabled_for(OrgStub()) is False  # provider disabled → never enabled regardless of consent
    assert owner.patch("/org", json={"ai_processing_consent": False}).json()["ai_processing_consented_at"] is None
    hist = owner.get("/quotes?q=zzz")  # smoke: unrelated call still fine
    assert hist.status_code == 200


def test_l10_weak_secret_refused_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("SECRET_KEY", "change-me-to-a-random-64-char-string-change-me-to-a-random-64-char")
    config.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config.get_settings()
    config.get_settings.cache_clear()


def test_new_tenant_members_do_not_see_other_tenants_users_via_member_list(app) -> None:  # type: ignore[no-untyped-def]
    t1 = make_tenant("Iota One", with_catalog=False)
    t2 = make_tenant("Kappa Two", with_catalog=False)
    a = login_as(app, t1.users["admin"])
    emails = {m["user"]["email"] for m in a.get("/org/members").json()}
    assert emails == set(t1.users.values()) and not (emails & set(t2.users.values()))
