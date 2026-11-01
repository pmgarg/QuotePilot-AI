import hashlib
from datetime import date, timedelta

import pytest

from tests.conftest import FIXTURES, AuthedClient, Tenant, login_as, make_tenant

pytestmark = pytest.mark.integration


def make_quote(ac: AuthedClient, customer_id: str, text: str | None = None) -> dict:  # type: ignore[type-arg]
    text = text or (FIXTURES / "rfq_clean.txt").read_text()
    rfq = ac.post("/rfqs", json={"text": text, "customer_id": customer_id}).json()
    for item in rfq["items"]:
        if item["selected_product_id"] is None and item["candidates"]:
            ac.patch(
                f"/rfqs/{rfq['id']}/items/{item['id']}",
                json={"selected_product_id": item["candidates"][0]["product_id"]},
            )
        elif item["selected_product_id"] is None:
            ac.delete(f"/rfqs/{rfq['id']}/items/{item['id']}")
    r = ac.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert r.status_code == 201, r.text
    return r.json()


def test_totals_tax_discount_rounding(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    lines = q["current_version"]["lines"]
    # org tax 5%; 500 x 0.85 = 425.00 → tax 21.25 → 446.25
    bolt = next(ln for ln in lines if ln["sku"] == "SKU-1001")
    assert (bolt["line_subtotal"], bolt["tax_amount"], bolt["line_total"]) == ("425", "21.25", "446.25")
    v = q["current_version"]
    assert sum(float(ln["line_total"]) for ln in lines) == pytest.approx(float(v["grand_total"]))
    # apply a 7.5% discount to the bolt line via edit
    payload = {
        "expected_lock_version": q["lock_version"],
        "lines": [
            {"id": ln["id"], "discount_pct": "7.5"} if ln["sku"] == "SKU-1001" else {"id": ln["id"]} for ln in lines
        ],
    }
    q2 = sales.patch(f"/quotes/{q['id']}", json=payload).json()
    bolt2 = next(ln for ln in q2["current_version"]["lines"] if ln["sku"] == "SKU-1001")
    assert (bolt2["discount_amount"], bolt2["tax_amount"], bolt2["line_total"]) == (
        "31.88",
        "19.66",
        "412.78",
    )  # 425*0.075=31.875→31.88; 393.12*0.05=19.656→19.66
    assert "cost_price" not in bolt2 and "margin_pct" not in bolt2  # sales cannot see costs


def test_currency_minor_units_jpy_and_kwd(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Yen Traders", currency="JPY", tax="10", min_margin="0")
    sales = login_as(app, t.users["sales"])
    q = make_quote(sales, str(t.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    assert (
        ln["line_subtotal"] == "9" and ln["tax_amount"] == "1" and ln["line_total"] == "10"
    )  # 8.5 → 9 (0 dp), tax 0.9 → 1
    t2 = make_tenant("Dinar Traders", currency="KWD", tax="0", min_margin="0")
    sales2 = login_as(app, t2.users["sales"])
    q2 = make_quote(sales2, str(t2.customer_id), "3 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    assert q2["current_version"]["lines"][0]["line_subtotal"] == "2.55"


def test_currency_mismatch_blocks_autofill(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt", "currency": "USD"}).json()
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote").json()
    ln = q["current_version"]["lines"][0]
    assert ln["unit_price"] is None and ln["price_source"] == "none"
    assert any(b["code"] == "missing_price" for b in q["blockers"])
    r = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert r.status_code == 409 and r.json()["error"] == "blocked"
    q2 = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.25"}]},
    ).json()
    assert q2["current_version"]["lines"][0]["price_source"] == "manual" and q2["blockers"] == []


def test_submit_approve_flow_with_separation_of_duties(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    readonly = login_as(app, tenant_a.users["readonly"])
    q = make_quote(sales, str(tenant_a.customer_id))
    lv = q["lock_version"]
    assert (
        sales.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv}).status_code == 403
    )  # not pending + wrong role
    r = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": lv})
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    lv = r.json()["lock_version"]
    assert sales.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv}).status_code == 403
    assert readonly.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv}).status_code == 403
    assert (
        sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": lv, "notes": "x"}).status_code == 409
    )  # not draft
    r = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv, "reason": "ok"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["current_version"]["status"] == "approved"
    assert r.json()["current_version"]["decisions"][0]["decision"] == "approved"
    lv = r.json()["lock_version"]
    assert (
        approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": lv}).status_code == 409
    )  # already decided


def test_self_approval_blocked_unless_org_allows(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("Solo Shop", min_margin="0")
    admin = login_as(app, t.users["admin"])
    q = make_quote(admin, str(t.customer_id))
    r = admin.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    deny = admin.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": r["lock_version"]})
    assert deny.status_code == 403 and deny.json()["action"] == "self_approval"
    owner = login_as(app, t.users["owner"])
    assert owner.patch("/org", json={"allow_self_approval": True}).status_code == 200
    assert (
        admin.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": r["lock_version"]}).status_code == 200
    )


def test_reject_returns_to_draft_with_reason(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    r = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    assert (
        approver.post(f"/quotes/{q['id']}/reject", json={"expected_lock_version": r["lock_version"]}).status_code == 422
    )
    rj = approver.post(
        f"/quotes/{q['id']}/reject", json={"expected_lock_version": r["lock_version"], "reason": "margin too thin"}
    ).json()
    assert rj["status"] == "draft" and rj["current_version"]["decisions"][-1]["decision"] == "rejected"


def test_margin_floor_requires_privileged_override(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    q = make_quote(sales, str(tenant_a.customer_id), "10 pcs SKU-1001 Hex bolt M8x40 zinc plated\n")
    ln = q["current_version"]["lines"][0]
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={"expected_lock_version": q["lock_version"], "lines": [{"id": ln["id"], "unit_price": "0.55"}]},
    ).json()  # cost 0.52 → ~5% margin < 12%
    assert any(b["code"] == "below_min_margin" and b["overridable_by"] == ["owner", "admin"] for b in q["blockers"])
    blocked = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]})
    assert blocked.status_code == 409
    assert (
        sales.post(
            f"/quotes/{q['id']}/lines/{ln['id']}/margin-override",
            json={"expected_lock_version": q["lock_version"], "reason": "strategic account"},
        ).status_code
        == 403
    )
    assert (
        admin.post(
            f"/quotes/{q['id']}/lines/{ln['id']}/margin-override",
            json={"expected_lock_version": q["lock_version"], "reason": "abc"},
        ).status_code
        == 422
    )
    r = admin.post(
        f"/quotes/{q['id']}/lines/{ln['id']}/margin-override",
        json={"expected_lock_version": q["lock_version"], "reason": "strategic account, approved by GM"},
    )
    assert r.status_code == 200 and r.json()["blockers"] == []
    priv_line = r.json()["current_version"]["lines"][0]
    assert (
        priv_line["margin_override_reason"]
        and priv_line["cost_price"] == "0.52"
        and priv_line["margin_pct"] is not None
    )
    q = sales.get(f"/quotes/{q['id']}").json()
    assert sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).status_code == 200


def test_concurrent_update_conflict(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    admin = login_as(app, tenant_a.users["admin"])
    q = make_quote(sales, str(tenant_a.customer_id))
    lv = q["lock_version"]
    assert sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": lv, "notes": "first"}).status_code == 200
    r = admin.patch(f"/quotes/{q['id']}", json={"expected_lock_version": lv, "notes": "second"})
    assert r.status_code == 409 and r.json()["error"] == "conflict" and r.json()["current_lock_version"] == lv + 1


def test_new_version_after_approval_keeps_history(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]}).json()
    assert (
        sales.post(f"/quotes/{q['id']}/versions", json={"expected_lock_version": a["lock_version"]}).status_code == 201
    )
    q2 = sales.get(f"/quotes/{q['id']}").json()
    assert q2["current_version_no"] == 2 and q2["status"] == "draft"
    assert [v["status"] for v in q2["versions"]] == ["approved", "draft"]
    assert len(q2["versions"][1]["lines"]) == len(q2["versions"][0]["lines"])
    hist = sales.get(f"/quotes/{q['id']}/history").json()
    actions = [e["action"] for e in hist["audit"]]
    assert {"quote.create", "quote.submit", "quote.approve", "quote.new_version"} <= set(actions)


def test_withdraw_and_expired(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    a = approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]}).json()
    assert (
        sales.post(
            f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": a["lock_version"], "reason": "x"}
        ).status_code
        == 403
    )
    w = approver.post(
        f"/quotes/{q['id']}/withdraw", json={"expected_lock_version": a["lock_version"], "reason": "customer cancelled"}
    )
    assert w.status_code == 200 and w.json()["status"] == "withdrawn"
    assert (
        approver.get(f"/quotes/{q['id']}/versions/1/pdf").status_code == 409
    )  # withdrawn is not exportable as current
    # expired: validity in the past blocks approval
    q2 = make_quote(sales, str(tenant_a.customer_id))
    past = (date.today() - timedelta(days=1)).isoformat()
    e = sales.patch(
        f"/quotes/{q2['id']}", json={"expected_lock_version": q2["lock_version"], "valid_until": past}
    ).json()
    assert any(b["code"] == "validity_expired" for b in e["blockers"])
    assert (
        sales.post(f"/quotes/{q2['id']}/submit", json={"expected_lock_version": e["lock_version"]}).status_code == 409
    )


def test_pdf_csv_exports_deterministic_and_role_aware(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    approver = login_as(app, tenant_a.users["approver"])
    q = make_quote(sales, str(tenant_a.customer_id))
    draft_pdf = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert (
        draft_pdf.status_code == 200 and b"NOT APPROVED" in draft_pdf.content or draft_pdf.content.startswith(b"%PDF")
    )
    s = sales.post(f"/quotes/{q['id']}/submit", json={"expected_lock_version": q["lock_version"]}).json()
    approver.post(f"/quotes/{q['id']}/approve", json={"expected_lock_version": s["lock_version"]})
    r1 = sales.post(f"/quotes/{q['id']}/versions/1/pdf").json()
    r2 = sales.post(f"/quotes/{q['id']}/versions/1/pdf").json()
    assert r1["sha256"] == r2["sha256"] and r1["status"] == "approved"
    dl = sales.get(f"/quotes/{q['id']}/versions/1/pdf")
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/pdf"
    assert hashlib.sha256(dl.content).hexdigest() == r1["sha256"]
    assert dl.headers["content-disposition"].endswith('.pdf"')
    csv_sales = sales.get(f"/quotes/{q['id']}/versions/1/csv")
    assert csv_sales.status_code == 200 and "cost_price" not in csv_sales.text
    csv_appr = approver.get(f"/quotes/{q['id']}/versions/1/csv")
    assert "cost_price" in csv_appr.text
    assert sales.get(f"/quotes/{q['id']}/versions/9/pdf").status_code == 404


def test_csv_export_neutralises_formulas(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    q = sales.patch(
        f"/quotes/{q['id']}",
        json={
            "expected_lock_version": q["lock_version"],
            "lines": [
                {"description": '=HYPERLINK("http://evil")', "quantity": "1", "unit_price": "1", "is_custom": True}
            ],
        },
    ).json()
    text = sales.get(f"/quotes/{q['id']}/versions/1/csv").text
    assert "\"'=HYPERLINK" in text


def test_quote_search_and_list(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    assert any(x["id"] == q["id"] for x in sales.get("/quotes").json())
    assert sales.get(f"/quotes?q={q['number']}").json()[0]["customer_name"] == "Alpha Trading Customer"
    assert sales.get("/quotes?q=Alpha").json()
    assert sales.get("/quotes?status=approved").json() == []
    assert sales.get("/quotes?q=%27%3B%20DROP%20TABLE%20quotes%3B--").status_code == 200


def test_audit_trail_for_every_mutation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = make_quote(sales, str(tenant_a.customer_id))
    sales.patch(f"/quotes/{q['id']}", json={"expected_lock_version": q["lock_version"], "notes": "n"})
    hist = sales.get(f"/quotes/{q['id']}/history").json()
    events = hist["audit"]
    assert all(e["actor_user_id"] == sales.user_id and e["request_id"] for e in events)
    assert {"quote.create", "quote.update"} <= {e["action"] for e in events}


def test_invalid_line_values_rejected(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    q = sales.post("/quotes", json={"customer_id": str(tenant_a.customer_id)}).json()
    for bad in (
        {"quantity": "0"},
        {"quantity": "-1"},
        {"discount_pct": "101"},
        {"tax_rate_pct": "-1"},
        {"unit_price": "-5"},
    ):
        r = sales.patch(
            f"/quotes/{q['id']}",
            json={
                "expected_lock_version": q["lock_version"],
                "lines": [{"description": "x", "quantity": "1", "unit_price": "1", "is_custom": True, **bad}],
            },
        )
        assert r.status_code == 422, bad
