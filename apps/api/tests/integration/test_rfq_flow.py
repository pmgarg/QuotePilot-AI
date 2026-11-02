import pytest

from tests.conftest import FIXTURES, Tenant, login_as, make_tenant, upload

pytestmark = pytest.mark.integration


def _items_by_line(rfq: dict) -> dict[int, dict]:  # type: ignore[type-arg]
    return {i["line_no"]: i for i in rfq["items"]}


def test_text_rfq_parsed_matched_and_sourced(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = sales.post(
        "/rfqs",
        json={
            "text": (FIXTURES / "rfq_clean.txt").read_text(),
            "customer_id": str(tenant_a.customer_id),
            "reference": "RFQ-118",
        },
    )
    assert r.status_code == 201, r.text
    rfq = r.json()
    assert rfq["status"] == "parsed" and rfq["currency"] == "AED"
    items = _items_by_line(rfq)
    assert items[1]["match_status"] == "exact" and items[1]["quantity"] == "500" and items[1]["unit_normalized"] == "EA"
    assert items[1]["selected_product_id"] == items[1]["candidates"][0]["product_id"]
    assert items[1]["source_ref"]["line"] > 0 and items[1]["raw_text"].startswith("1. 500 pcs SKU-1001")
    assert items[3]["match_status"] in ("likely", "ambiguous") and items[3]["selected_product_id"] is None
    assert all(c["reason"] for i in items.values() for c in i["candidates"])


@pytest.mark.parametrize("name", ["rfq_clean.pdf", "rfq_clean.xlsx", "rfq_clean.csv"])
def test_file_rfqs_parse(app, tenant_a: Tenant, name: str) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", name, (FIXTURES / name).read_bytes(), {"customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 201, r.text
    rfq = r.json()
    assert rfq["status"] == "parsed" and rfq["source_file_id"]
    exact = [i for i in rfq["items"] if i["match_status"] == "exact"]
    assert exact, rfq["items"]
    # original is downloadable by the same tenant only
    assert sales.get(f"/files/{rfq['source_file_id']}").status_code == 200


def test_ambiguous_unknown_duplicate_negative_and_unit_mismatch(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": (FIXTURES / "rfq_ambiguous.txt").read_text()}).json()
    items = _items_by_line(rfq)
    bearing = next(i for i in items.values() if "bearing" in i["description"].lower())
    assert bearing["match_status"] == "ambiguous" and len(bearing["candidates"]) >= 2
    unknown = next(i for i in items.values() if "SKU-9999" in i["raw_text"])
    assert unknown["match_status"] == "unmatched" and unknown["candidates"] == []
    dups = [i for i in items.values() if "SKU-1001" in i["raw_text"]]
    assert [d["is_duplicate"] for d in dups] == [False, True]
    washer = next(i for i in items.values() if "SKU-1004" in i["raw_text"])
    assert washer["match_status"] == "exact" and washer["unit_mismatch"] is True  # box vs EA
    neg = next(i for i in items.values() if "-5 pcs" in i["raw_text"] or "SKU-1002" in i["raw_text"])
    assert neg["quantity"] is None or not neg["quantity"].startswith("-")
    # generation is blocked until resolved, with precise blockers
    r = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert r.status_code == 409 and r.json()["error"] == "blocked"
    codes = {b["code"] for b in r.json()["blockers"]}
    assert {"line_unresolved", "duplicate_unconfirmed", "unit_mismatch_unconfirmed"} <= codes


def test_review_edits_and_generation(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post(
        "/rfqs", json={"text": (FIXTURES / "rfq_ambiguous.txt").read_text(), "customer_id": str(tenant_a.customer_id)}
    ).json()
    items = _items_by_line(rfq)
    bearing = next(i for i in items.values() if "bearing" in i["description"].lower())
    chosen = bearing["candidates"][0]["product_id"]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{bearing['id']}", json={"selected_product_id": chosen})
    assert r.status_code == 200 and r.json()["match_status"] == "resolved"
    gloves = next(i for i in items.values() if "gloves" in i["description"].lower())
    assert (
        sales.patch(
            f"/rfqs/{rfq['id']}/items/{gloves['id']}",
            json={"selected_product_id": gloves["candidates"][0]["product_id"]},
        ).status_code
        == 200
    )
    unknown = next(i for i in items.values() if "SKU-9999" in i["raw_text"])
    assert sales.delete(f"/rfqs/{rfq['id']}/items/{unknown['id']}").status_code == 204
    dup = [i for i in items.values() if "SKU-1001" in i["raw_text"]][1]
    assert sales.delete(f"/rfqs/{rfq['id']}/items/{dup['id']}").status_code == 204
    washer = next(i for i in items.values() if "SKU-1004" in i["raw_text"])
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{washer['id']}", json={"unit_confirmed": True}).status_code == 200
    neg = next(i for i in items.values() if "SKU-1002" in i["raw_text"])
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{neg['id']}", json={"quantity": "-3"}).status_code == 422
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{neg['id']}", json={"quantity": "0"}).status_code == 422
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{neg['id']}", json={"quantity": "5"}).status_code == 200
    added = sales.post(
        f"/rfqs/{rfq['id']}/items",
        json={"description": "Hex nut M8", "quantity": "100", "unit": "pcs", "sku_hint": "SKU-1003"},
    )
    assert added.status_code == 201 and added.json()["match_status"] == "exact"
    r = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert r.status_code == 201, r.text
    quote = r.json()
    assert quote["number"].startswith("Q-") and quote["status"] == "draft" and quote["current_version_no"] == 1
    lines = quote["current_version"]["lines"]
    assert len(lines) == 6 and all(ln["price_source"] == "catalog" and ln["unit_price"] for ln in lines)
    assert quote["blockers"] == []
    assert sales.get(f"/rfqs/{rfq['id']}").json()["status"] == "quoted"
    assert (
        sales.patch(f"/rfqs/{rfq['id']}/items/{washer['id']}", json={"quantity": "9"}).status_code == 409
    )  # locked after quoting


@pytest.mark.parametrize(("name", "status"), [("not_supported.docx", 415), ("binary.exe", 415), ("macro.xlsm", 415)])
def test_unsupported_files_rejected(app, tenant_a: Tenant, name: str, status: int) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", name, (FIXTURES / name).read_bytes())
    assert r.status_code == status, r.text
    assert sales.get("/rfqs").json() == []  # nothing persisted


def test_corrupted_pdf_fails_cleanly_and_can_retry(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", "corrupted.pdf", (FIXTURES / "corrupted.pdf").read_bytes())
    assert r.status_code == 201, r.text
    rfq = r.json()
    assert rfq["status"] == "failed" and rfq["failure_reason"] and rfq["items"] == []
    r2 = sales.post(f"/rfqs/{rfq['id']}/reparse")
    assert r2.status_code == 200 and r2.json()["status"] == "failed"


def test_corrupted_and_bomb_xlsx_rejected_before_storage(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    assert upload(sales, "/rfqs", "corrupted.xlsx", (FIXTURES / "corrupted.xlsx").read_bytes()).status_code == 422
    assert upload(sales, "/rfqs", "zip_bomb.xlsx", (FIXTURES / "zip_bomb.xlsx").read_bytes()).status_code == 413
    assert sales.get("/rfqs").json() == []


def test_scanned_pdf_flagged_unsupported(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = upload(sales, "/rfqs", "scan.pdf", (FIXTURES / "scanned_no_text.pdf").read_bytes()).json()
    assert rfq["status"] == "failed" and "unsupported_scanned" in rfq["failure_reason"]


def test_javascript_pdf_treated_as_inert_text(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = upload(sales, "/rfqs", "js.pdf", (FIXTURES / "javascript_openaction.pdf").read_bytes()).json()
    assert rfq["status"] in ("parsed", "failed")
    if rfq["status"] == "parsed":
        assert any("SKU-1001" in i["raw_text"] for i in rfq["items"])


def test_hidden_prompt_pdf_does_not_change_anything(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = upload(
        sales,
        "/rfqs",
        "hidden.pdf",
        (FIXTURES / "rfq_hidden_prompt.pdf").read_bytes(),
        {"customer_id": str(tenant_a.customer_id)},
    ).json()
    assert rfq["status"] == "parsed"
    for i in rfq["items"]:
        if "ignore previous" in i["raw_text"].lower():
            assert i["match_status"] in ("unmatched", "ambiguous")  # just text
    exact = [i for i in rfq["items"] if i["match_status"] == "exact"]
    assert len(exact) >= 2


def test_pasted_text_limits(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    assert sales.post("/rfqs", json={"text": "   "}).status_code == 422
    assert sales.post("/rfqs", json={"text": "x" * 150_000}).status_code == 413


def test_rfq_without_active_catalog_is_unmatched(app) -> None:  # type: ignore[no-untyped-def]
    t = make_tenant("No Catalog Co", with_catalog=False)
    sales = login_as(app, t.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt"}).json()
    assert rfq["status"] == "parsed" and rfq["items"][0]["match_status"] == "unmatched"
    assert "no_active_catalog" in rfq["items"][0]["flags"]


def test_readonly_cannot_create_rfq(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    ro = login_as(app, tenant_a.users["readonly"])
    assert ro.post("/rfqs", json={"text": "10 pcs SKU-1001"}).status_code == 403
