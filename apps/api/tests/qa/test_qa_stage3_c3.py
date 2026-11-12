"""QA re-check of Stage 3 fix rounds 2 and 3 (release review 1, condition C3).

Written from the requirements (erased customers are not processed any further and their name is withheld from
roles without ``manage_retention``; confirm and CSV replay are race-safe), not from the implementation.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text

from app.db.session import get_sessionmaker, set_tenant
from tests.conftest import Tenant, login_as, upload
from tests.integration.test_stage3_intelligence import approved_quote

pytestmark = [pytest.mark.integration]

ROLES = ("owner", "admin", "sales", "approver", "readonly")
RETENTION_ROLES = {"owner", "admin"}  # manage_retention
PROPOSER_ROLES = ("owner", "admin", "sales", "approver")  # edit_quotes


def _race(*calls: Callable[[], Any]) -> list[Any]:
    barrier = threading.Barrier(len(calls))
    results: list[Any] = [None] * len(calls)

    def run(i: int, fn: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def _request_and_approve_erasure(app: Any, tenant: Tenant) -> None:
    sales = login_as(app, tenant.users["sales"])
    owner = login_as(app, tenant.users["owner"])
    dr = sales.post("/retention/requests", json={"customer_id": str(tenant.customer_id), "reason": "erasure"})
    assert dr.status_code == 201, dr.text
    assert owner.post(f"/retention/requests/{dr.json()['id']}/approve", json={}).status_code == 200


# ---------------------------------------------------------------- (a) no order from a quote of an erased customer
def test_c3a_quote_of_a_customer_pending_erasure_cannot_become_an_order_for_any_role(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    quotes = {role: approved_quote(app, tenant_a)[2] for role in ROLES}  # one approved quote per role, before erasure
    _request_and_approve_erasure(app, tenant_a)
    for role in ROLES:
        r = login_as(app, tenant_a.users[role]).post(f"/quotes/{quotes[role]['id']}/orders", json={})
        if role in PROPOSER_ROLES:
            assert r.status_code == 404, (role, r.status_code, r.text[:150])
        else:
            assert r.status_code == 403, (role, r.status_code, r.text[:150])
        assert tenant_a.name.split()[0] not in r.text or r.status_code == 403, role  # no customer name in the refusal
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        n = db.scalar(text("SELECT count(*) FROM orders WHERE source <> 'import'"))
        db.rollback()
    assert n == 0, "an order was created for a customer pending erasure"


# ------------------------------------------------- (b) name visibility per role: list, search, customers, detail
def test_c3b_erased_customer_name_is_visible_only_to_manage_retention_roles(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, _, q = approved_quote(app, tenant_a)
    name = f"{tenant_a.name} Customer"
    for role in ROLES:  # precondition: before the approval every role sees the name
        assert login_as(app, tenant_a.users[role]).get("/quotes").json()[0]["customer_name"] == name, role
    _request_and_approve_erasure(app, tenant_a)
    for role in ROLES:
        ac = login_as(app, tenant_a.users[role])
        rows = ac.get("/quotes").json()
        assert [r["id"] for r in rows] == [q["id"]], role  # the quote itself stays listed
        shown = rows[0]["customer_name"]
        by_name = ac.get("/quotes", params={"q": "Customer"}).json()
        by_number = ac.get("/quotes", params={"q": q["number"]}).json()
        assert [r["id"] for r in by_number] == [q["id"]], role
        if role in RETENTION_ROLES:
            assert shown == name, role
            assert [r["id"] for r in by_name] == [q["id"]], role
        else:
            assert shown is None, (role, shown)
            assert by_name == [], role  # a name search does not act as an existence oracle
            assert all(name not in str(r) for r in by_number), role
        # customer list and name/reference search: a customer pending erasure is hidden from every role
        assert ac.get("/customers").json() == [], role
        assert ac.get("/customers", params={"q": "Alpha"}).json() == [], role
        assert ac.get("/customers", params={"q": "CUST-1"}).json() == [], role


def test_c3b_quote_detail_of_an_erased_customer_does_not_leak_the_name(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _, _, q = approved_quote(app, tenant_a)
    name = f"{tenant_a.name} Customer"
    _request_and_approve_erasure(app, tenant_a)
    for role in ROLES:
        r = login_as(app, tenant_a.users[role]).get(f"/quotes/{q['id']}")
        assert r.status_code == 200, (role, r.status_code)  # the quote is a business record and stays readable
        if role not in RETENTION_ROLES:
            assert name not in r.text, f"{role}: quote detail leaks the erased customer's name"


# ------------------------------------------------------------------------------- (c) concurrent confirm, 6 rounds
def test_c3c_concurrent_confirm_of_one_order_is_recorded_exactly_once_over_six_rounds(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    approver = login_as(app, tenant_a.users["approver"])
    owner = login_as(app, tenant_a.users["owner"])
    for rnd in range(6):
        r = upload(
            admin,
            f"/data/import/orders?idempotency_key=c3c-{rnd}",
            "o.csv",
            f"order_ref,sku,quantity,unit_price\r\nC3C-{rnd},A,1,1\r\n".encode(),
        )
        assert r.status_code == 201 and r.json()["applied"] == 1, r.text[:200]
        oid = next(o["id"] for o in admin.get("/orders").json() if o["status"] == "proposed")
        res = _race(
            lambda oid=oid: approver.post(f"/orders/{oid}/confirm", json={}).status_code,
            lambda oid=oid: owner.post(f"/orders/{oid}/confirm", json={}).status_code,
        )
        assert sorted(res, key=str) == [200, 409], (rnd, res)
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            kinds = db.scalars(text("SELECT kind FROM order_events WHERE order_id = :o"), {"o": oid}).all()
            audits = db.scalar(
                text("SELECT count(*) FROM audit_events WHERE action = 'order.confirmed' AND entity_id = :o"),
                {"o": oid},
            )
            status = db.scalar(text("SELECT status FROM orders WHERE id = :o"), {"o": oid})
            db.rollback()
        assert kinds.count("confirmed") == 1, (rnd, kinds)
        assert audits == 1, (rnd, audits)
        assert status == "confirmed", (rnd, status)


# -------------------------------------------------------------------- (d) CSV import replay race, 6 rounds
def test_c3d_same_csv_import_key_uploaded_concurrently_applies_once_and_replays(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    admin2 = login_as(app, tenant_a.users["owner"])
    for rnd in range(6):
        key = f"c3d-{rnd}"
        body = f"order_ref,sku,quantity,unit_price\r\nC3D-{rnd},A,1,1\r\n".encode()
        res = _race(
            lambda key=key, body=body: upload(admin, f"/data/import/orders?idempotency_key={key}", "o.csv", body),
            lambda key=key, body=body: upload(admin2, f"/data/import/orders?idempotency_key={key}", "o.csv", body),
        )
        assert all(not isinstance(x, Exception) for x in res), (rnd, res)
        assert [x.status_code for x in res] == [201, 201], (rnd, [(x.status_code, x.text[:120]) for x in res])
        assert res[0].json()["id"] == res[1].json()["id"], rnd  # both answers describe the same run
        assert res[0].json()["applied"] == 1 and res[1].json()["applied"] == 1, rnd
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            orders = db.scalar(text("SELECT count(*) FROM orders WHERE external_ref = :r"), {"r": f"C3D-{rnd}"})
            runs = db.scalar(text("SELECT count(*) FROM sync_runs WHERE idempotency_key = :k"), {"k": key})
            db.rollback()
        assert orders == 1 and runs == 1, (rnd, orders, runs)
        # afterwards: same key, different payload -> 409, nothing from the second payload is applied
        other = upload(
            admin,
            f"/data/import/orders?idempotency_key={key}",
            "o.csv",
            f"order_ref,sku,quantity,unit_price\r\nC3D-OTHER-{rnd},A,1,1\r\n".encode(),
        )
        assert other.status_code == 409, (rnd, other.status_code, other.text[:150])
        with get_sessionmaker()() as db:
            set_tenant(db, tenant_a.org_id, None)
            leaked = db.scalar(text("SELECT count(*) FROM orders WHERE external_ref = :r"), {"r": f"C3D-OTHER-{rnd}"})
            db.rollback()
        assert leaked == 0, rnd
    assert uuid.UUID(res[0].json()["id"])
