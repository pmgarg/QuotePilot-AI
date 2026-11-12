"""QA (independent) — durability/read-your-writes of mutating endpoints, found while running the Stage 3 browser
suite (a quote showed "Submitted for approval." next to a stale *draft* status; the database already held
``pending_approval``).

Requirement basis: CLAUDE.md "Every mutating endpoint writes an audit_events row within the same transaction" and
"Honest status" — a 2xx answer to a mutation must mean the transaction is committed. If the commit happens after the
response has been sent, (a) the next request of the same user can read the old state and (b) a commit failure can
no longer be reported to the caller.

The Starlette TestClient only returns once the whole ASGI call has finished, so ordinary API tests cannot see the
window; this test probes the database from a second connection at the instant the last response byte is sent.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import get_sessionmaker, set_tenant
from tests.conftest import AuthedClient, Tenant
from tests.qa.test_qa_stage3_helpers import approved_quote

pytestmark = pytest.mark.integration


class ProbeAtResponseEnd:
    """ASGI wrapper: runs ``probe(scope)`` right after the final ``http.response.body`` message is sent."""

    def __init__(self, inner: Any, probe: Any) -> None:
        self.inner = inner
        self.probe = probe

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        async def send_wrapper(message: dict[str, Any]) -> None:
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self.probe(scope)

        await self.inner(scope, receive, send_wrapper)


def _count(org_id: Any, statement: str, params: dict[str, Any]) -> int:
    with get_sessionmaker()() as db:  # a second connection: sees committed data only
        set_tenant(db, org_id, None)
        n = db.execute(text(statement), params).scalar()
        db.rollback()
    return int(n or 0)


def test_created_record_is_committed_when_the_201_is_sent(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    seen: list[int] = []

    def probe(scope: dict[str, Any]) -> None:
        if scope["method"] == "POST" and scope["path"].endswith("/customers"):
            seen.append(_count(tenant_a.org_id, "SELECT count(*) FROM customers WHERE name = :n", {"n": "RYW Co"}))

    sales = AuthedClient(
        TestClient(ProbeAtResponseEnd(app, probe), base_url="http://testserver"), tenant_a.users["sales"]
    )
    r = sales.post("/customers", json={"name": "RYW Co"})
    assert r.status_code == 201, r.text
    assert seen == [1], (
        f"POST /customers answered 201 but the row was not yet committed when the response ended (visible rows at "
        f"that instant: {seen}); a follow-up GET can return 404/stale data and a commit failure would go unreported"
    )


def test_order_confirmation_is_committed_when_the_200_is_sent(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    order = sales.post(f"/quotes/{q['id']}/orders", json={}).json()
    seen: list[tuple[int, int]] = []

    def probe(scope: dict[str, Any]) -> None:
        if scope["method"] == "POST" and scope["path"].endswith("/confirm"):
            confirmed = _count(
                tenant_a.org_id,
                "SELECT count(*) FROM orders WHERE id = :i AND status = 'confirmed'",
                {"i": order["id"]},
            )
            audited = _count(
                tenant_a.org_id,
                "SELECT count(*) FROM audit_events WHERE entity_id = :i AND action = 'order.confirmed'",
                {"i": order["id"]},
            )
            seen.append((confirmed, audited))

    approver = AuthedClient(
        TestClient(ProbeAtResponseEnd(app, probe), base_url="http://testserver"), tenant_a.users["approver"]
    )
    r = approver.post(f"/orders/{order['id']}/confirm", json={})
    assert r.status_code == 200 and r.json()["status"] == "confirmed", r.text
    assert seen == [(1, 1)], (
        f"order confirm answered 200 'confirmed' but (confirmed rows, audit rows) committed at that instant = {seen}"
    )
