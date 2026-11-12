"""QA (independent) — S3-F04 KPI dashboard: S3-A01 (figures match hand-computed fixtures) and S3-A03
(multi-currency aggregation: reporting currency + explicit FX table + "unconverted" bucket, nothing guessed).

Fixture arithmetic (tenant AED, tax 5 %): one-line quote 500 × 0.85 = 425.00 + 21.25 = 446.25.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.db.base import utcnow
from tests.conftest import Tenant, login_as
from tests.integration.test_quotes import make_quote
from tests.qa.test_qa_stage3_helpers import (
    ONE_LINE_RFQ,
    ONE_LINE_TOTAL,
    approved_quote,
    run_sql,
    seed_confirmed_order,
    transition,
)

pytestmark = pytest.mark.integration
TODAY = utcnow().date()


def kpis(ac, date_from: date | None = None, date_to: date | None = None):  # type: ignore[no-untyped-def]
    qs = []
    if date_from:
        qs.append(f"date_from={date_from.isoformat()}")
    if date_to:
        qs.append(f"date_to={date_to.isoformat()}")
    r = ac.get("/kpis" + (f"?{'&'.join(qs)}" if qs else ""))
    assert r.status_code == 200, r.text
    return r.json()


def test_kpi_figures_match_hand_computed_fixture(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    owner = login_as(app, tenant_a.users["owner"])
    # Q1 won, Q2 lost, Q3 withdrawn (terminal, not won), Q4 pending AED, Q5 pending USD (no rate), Q6 draft,
    # Q7 approved but expired
    _, _, q1 = approved_quote(app, tenant_a, sales=sales, approver=approver)
    q1 = transition(sales, q1, "sent")
    transition(sales, q1, "won", "signed PO by e-mail")
    _, _, q2 = approved_quote(app, tenant_a, sales=sales, approver=approver)
    transition(sales, q2, "lost", "competitor cheaper")
    _, _, q3 = approved_quote(app, tenant_a, sales=sales, approver=approver)
    w = owner.post(f"/quotes/{q3['id']}/withdraw", json={"expected_lock_version": q3["lock_version"], "reason": "typo"})
    assert w.status_code == 200, w.text
    _, _, q4 = approved_quote(app, tenant_a, sales=sales, approver=approver)
    _, _, q5 = approved_quote(app, tenant_a, sales=sales, approver=approver)
    run_sql(tenant_a.org_id, "UPDATE quotes SET currency = 'USD' WHERE id = :id", {"id": q5["id"]})
    make_quote(sales, str(tenant_a.customer_id), ONE_LINE_RFQ)  # Q6 draft
    _, _, q7 = approved_quote(app, tenant_a, sales=sales, approver=approver)
    run_sql(tenant_a.org_id, "UPDATE quotes SET valid_until = current_date - 1 WHERE id = :id", {"id": q7["id"]})

    k = kpis(sales)
    m = k["metrics"]
    assert k["reporting_currency"] == "AED"
    assert m["quote_volume"]["value"] == 7
    # closed = won 1 + lost 1 + withdrawn 1 = 3; won / closed = 33.3 %
    assert m["conversion_rate"] == {"value": "33.3", "won": 1, "closed": 3}
    # pending = Q4 (AED) only converted; Q5 USD unconverted; Q7 expired excluded; Q6 draft not approved
    assert m["pending_value"] == {"value": str(ONE_LINE_TOTAL), "count": 2, "currency": "AED"}
    assert k["unconverted"] == {"USD": str(ONE_LINE_TOTAL)} and k["unconverted_count"] == 1
    assert m["won_value"] == {"value": str(ONE_LINE_TOTAL), "currency": "AED"}
    assert m["repeat_order_rate"] == {"value": None, "repeat_customers": 0, "ordering_customers": 0}
    # every metric has a written definition and the range is echoed back
    assert set(k["definitions"]) == set(m) and all(len(d) > 30 for d in k["definitions"].values())
    assert k["date_to"] == TODAY.isoformat() and k["date_from"] == (TODAY - timedelta(days=90)).isoformat()
    # readonly sees the same figures (no cost/margin in KPIs)
    assert login_as(app, tenant_a.users["readonly"]).get("/kpis").json()["metrics"] == m
    assert not any("margin" in key or "cost" in key for key in m)


def test_pending_value_includes_every_open_approved_quote_per_its_definition(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """Definition (returned by the API): pending value = approved quotes 'still open (not won, lost, withdrawn or
    expired)'. A quote that was *sent* to the customer is still open and still worth its total."""
    sales, _, q_sent = approved_quote(app, tenant_a)
    _, _, q_open = approved_quote(app, tenant_a, sales=sales)
    transition(sales, q_sent, "sent")
    k = kpis(sales)
    assert "not won, lost, withdrawn or expired" in k["definitions"]["pending_value"]
    assert k["metrics"]["pending_value"]["count"] == 2, k["metrics"]["pending_value"]
    assert Decimal(k["metrics"]["pending_value"]["value"]) == ONE_LINE_TOTAL * 2
    # and after acknowledgement it is still open
    transition(sales, sales.get(f"/quotes/{q_sent['id']}").json(), "acknowledged", "customer replied")
    assert kpis(sales)["metrics"]["pending_value"]["count"] == 2
    assert q_open["id"]


def test_date_range_boundaries_and_validation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    yesterday, tomorrow = TODAY - timedelta(days=1), TODAY + timedelta(days=1)
    assert kpis(sales, TODAY, TODAY)["metrics"]["quote_volume"]["value"] == 1  # inclusive on both ends
    assert kpis(sales, yesterday, yesterday)["metrics"]["quote_volume"]["value"] == 0
    assert kpis(sales, tomorrow, tomorrow)["metrics"]["quote_volume"]["value"] == 0
    # a quote created at 23:59:59 on date_to still counts; one at 00:00:00 on date_to + 1 does not
    run_sql(
        tenant_a.org_id,
        "UPDATE quotes SET created_at = (CAST(:d AS date) + time '23:59:59') AT TIME ZONE 'UTC' WHERE id = :id",
        {"d": yesterday.isoformat(), "id": q["id"]},
    )
    assert kpis(sales, yesterday, yesterday)["metrics"]["quote_volume"]["value"] == 1
    assert kpis(sales, TODAY, TODAY)["metrics"]["quote_volume"]["value"] == 0
    # pending value is "as of now" and not range-bound; the range only limits creation-dated metrics
    assert kpis(sales, TODAY, TODAY)["metrics"]["pending_value"]["count"] == 1
    # validation: inverted range, > 3 years, malformed
    assert sales.get(f"/kpis?date_from={TODAY}&date_to={yesterday}").status_code == 422
    assert sales.get(f"/kpis?date_from={TODAY - timedelta(days=1098)}&date_to={TODAY}").status_code == 200
    assert sales.get(f"/kpis?date_from={TODAY - timedelta(days=1099)}&date_to={TODAY}").status_code == 422
    assert sales.get("/kpis?date_from=not-a-date").status_code == 422


def test_fx_rule_uses_the_latest_rate_on_or_before_the_quote_date(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, won = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    won = transition(sales, won, "sent")
    transition(sales, won, "won", "PO received")
    two_days_ago = TODAY - timedelta(days=2)
    run_sql(
        tenant_a.org_id,
        "UPDATE quotes SET currency = 'USD', created_at = created_at - interval '2 days' WHERE id = :id",
        {"id": won["id"]},
    )
    _, _, pending = approved_quote(app, tenant_a, sales=sales)
    run_sql(tenant_a.org_id, "UPDATE quotes SET currency = 'USD' WHERE id = :id", {"id": pending["id"]})
    k = kpis(sales)
    assert k["metrics"]["won_value"]["value"] == "0.00" or Decimal(k["metrics"]["won_value"]["value"]) == 0
    assert k["unconverted"] == {"USD": str(ONE_LINE_TOTAL * 2)} and k["unconverted_count"] == 2
    # a rate dated *after* the won quote converts the pending quote (valued today) but not the won one
    r = admin.post(
        "/fx-rates",
        json={
            "base_currency": "usd",
            "quote_currency": "aed",
            "rate": "3.6725",
            "as_of": (TODAY - timedelta(days=1)).isoformat(),
        },
    )
    assert r.status_code == 201 and r.json()["base_currency"] == "USD", r.text
    k = kpis(sales)
    converted = (ONE_LINE_TOTAL * Decimal("3.6725")).quantize(Decimal("0.01"))  # 1638.85
    assert converted == Decimal("1638.85")
    assert Decimal(k["metrics"]["pending_value"]["value"]) == converted and k["metrics"]["pending_value"]["count"] == 1
    assert Decimal(k["metrics"]["won_value"]["value"]) == 0 and k["unconverted"] == {"USD": str(ONE_LINE_TOTAL)}
    # an older rate (on/before the quote date) converts the won quote with *that* rate, not the newer one
    r = admin.post(
        "/fx-rates",
        json={"base_currency": "USD", "quote_currency": "AED", "rate": "3.6700", "as_of": two_days_ago.isoformat()},
    )
    assert r.status_code == 201
    k = kpis(sales)
    assert Decimal(k["metrics"]["won_value"]["value"]) == (ONE_LINE_TOTAL * Decimal("3.67")).quantize(Decimal("0.01"))
    assert Decimal(k["metrics"]["pending_value"]["value"]) == converted  # today → latest rate (3.6725)
    assert k["unconverted"] == {} and k["unconverted_count"] == 0


def test_fx_inverse_pair_and_table_validation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, _, q = approved_quote(app, tenant_a)
    admin = login_as(app, tenant_a.users["admin"])
    run_sql(tenant_a.org_id, "UPDATE quotes SET currency = 'EUR' WHERE id = :id", {"id": q["id"]})
    # only the inverse pair (AED→EUR) exists → 1 / rate is used, quantised in the reporting currency
    assert (
        admin.post(
            "/fx-rates",
            json={"base_currency": "AED", "quote_currency": "EUR", "rate": "0.25", "as_of": TODAY.isoformat()},
        ).status_code
        == 201
    )
    k = kpis(sales)
    assert Decimal(k["metrics"]["pending_value"]["value"]) == (ONE_LINE_TOTAL / Decimal("0.25")).quantize(
        Decimal("0.01")
    )
    assert k["unconverted"] == {}
    bad = [
        ({"base_currency": "EUR", "quote_currency": "EUR", "rate": "1", "as_of": TODAY.isoformat()}, 422),
        ({"base_currency": "XXX", "quote_currency": "AED", "rate": "1", "as_of": TODAY.isoformat()}, 422),
        ({"base_currency": "EUR", "quote_currency": "AED", "rate": "0", "as_of": TODAY.isoformat()}, 422),
        ({"base_currency": "EUR", "quote_currency": "AED", "rate": "-1", "as_of": TODAY.isoformat()}, 422),
        (
            {
                "base_currency": "EUR",
                "quote_currency": "AED",
                "rate": "1",
                "as_of": (TODAY + timedelta(days=1)).isoformat(),
            },
            422,
        ),
        ({"base_currency": "AED", "quote_currency": "EUR", "rate": "0.26", "as_of": TODAY.isoformat()}, 409),
    ]
    for payload, status in bad:
        assert admin.post("/fx-rates", json=payload).status_code == status, payload
    # the table is readable by everyone in the tenant, writable by owner/admin only
    assert len(sales.get("/fx-rates").json()) == 1
    for role in ("sales", "approver", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        assert (
            ac.post(
                "/fx-rates",
                json={"base_currency": "GBP", "quote_currency": "AED", "rate": "4.7", "as_of": TODAY.isoformat()},
            ).status_code
            == 403
        ), role
    assert len(admin.get("/fx-rates").json()) == 1  # nothing slipped through


def test_response_and_processing_time_from_recorded_timestamps(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, qa = approved_quote(app, tenant_a)
    _, _, qb = approved_quote(app, tenant_a, sales=sales, approver=approver)
    unapproved = make_quote(sales, str(tenant_a.customer_id), ONE_LINE_RFQ)  # never submitted: excluded from samples
    # backdate recorded facts: quote A created 4 h before approval, B 2 h; RFQ A created 6 h before the quote was
    # submitted, RFQ B 2 h → medians 3.0 h (processing) and 4.0 h (response)
    for q, hours in ((qa, 4), (qb, 2)):
        run_sql(
            tenant_a.org_id,
            "UPDATE quotes SET created_at = created_at - make_interval(hours => :h) WHERE id = :id",
            {"h": hours, "id": q["id"]},
        )
    for q, hours in ((qa, 6), (qb, 2)):
        run_sql(
            tenant_a.org_id,
            "UPDATE rfqs SET created_at = created_at - make_interval(hours => :h) WHERE id = (SELECT rfq_id FROM quotes WHERE id = :id)",
            {"h": hours, "id": q["id"]},
        )
    m = kpis(sales, TODAY - timedelta(days=2), TODAY)["metrics"]
    assert m["processing_time_hours"]["sample"] == 2 and m["response_time_hours"]["sample"] == 2
    assert m["processing_time_hours"]["value"] == pytest.approx(3.0, abs=0.05)
    assert m["response_time_hours"]["value"] == pytest.approx(4.0, abs=0.05)
    assert m["quote_volume"]["value"] == 3 and unapproved["id"]
    # no facts → null, never zero (must not pretend untracked events occurred)
    empty = kpis(sales, TODAY - timedelta(days=400), TODAY - timedelta(days=300))["metrics"]
    assert empty["processing_time_hours"] == {"value": None, "sample": 0}
    assert empty["response_time_hours"] == {"value": None, "sample": 0}
    assert empty["conversion_rate"]["value"] is None


def test_repeat_order_rate_counts_customers_by_orders_confirmed_in_range(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    c2 = sales.post("/customers", json={"name": "Second Buyer"}).json()
    c3 = sales.post("/customers", json={"name": "Third Buyer"}).json()
    import uuid as _uuid

    seed_confirmed_order(tenant_a, customer_id=tenant_a.customer_id, days_ago=10)
    seed_confirmed_order(tenant_a, customer_id=tenant_a.customer_id, days_ago=40)
    seed_confirmed_order(tenant_a, customer_id=tenant_a.customer_id, days_ago=100)
    seed_confirmed_order(tenant_a, customer_id=_uuid.UUID(c2["id"]), days_ago=5)
    seed_confirmed_order(tenant_a, customer_id=_uuid.UUID(c3["id"]), days_ago=100)
    # default 90-day window: A has 2 orders, c2 has 1 → 1/2 repeaters
    m = kpis(sales)["metrics"]["repeat_order_rate"]
    assert m == {"value": "50.0", "repeat_customers": 1, "ordering_customers": 2}
    # 120-day window: A 3, c2 1, c3 1 → 1/3
    m = kpis(sales, TODAY - timedelta(days=120), TODAY)["metrics"]["repeat_order_rate"]
    assert m == {"value": "33.3", "repeat_customers": 1, "ordering_customers": 3}
    # proposed (unconfirmed) orders never count: an import creates proposed orders only
    admin = login_as(app, tenant_a.users["admin"])
    csv_in = (
        b"order_ref,customer_reference,sku,quantity,unit_price\r\nPO-X,,SKU-1001,1,0.85\r\nPO-Y,,SKU-1001,1,0.85\r\n"
    )
    r = admin.post("/data/import/orders?idempotency_key=k1", files={"file": ("o.csv", csv_in, "text/csv")}, data={})
    assert r.status_code == 201 and r.json()["applied"] == 2
    assert kpis(sales)["metrics"]["repeat_order_rate"] == {
        "value": "50.0",
        "repeat_customers": 1,
        "ordering_customers": 2,
    }
