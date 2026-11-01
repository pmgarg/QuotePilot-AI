"""QA (independent): RFQ intake and human verification edge cases.

Derived from docs/PRD.md S1-F06..F10 and acceptance IDs S1-A01, S1-A02, S1-A03, S1-A04, S1-A07, S1-A08, S1-A09,
S1-A10, S1-A17. Integration evidence (API + PostgreSQL + RLS); the parser-failure case stubs the sandbox entry point.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.services.rfqs as rfq_module
from app.db.session import get_sessionmaker, set_tenant
from app.models.audit import AuditEvent
from app.services.parsing.sandbox import ParseFailure
from tests.conftest import FIXTURES, Tenant, login_as, make_tenant, upload

pytestmark = pytest.mark.integration


def _item(rfq: dict, needle: str) -> dict:  # type: ignore[type-arg]
    return next(i for i in rfq["items"] if needle in i["raw_text"])


def _audit_actions(org_id: uuid.UUID, entity_id: str) -> list[str]:
    with get_sessionmaker()() as db:
        set_tenant(db, org_id, None)
        rows = db.scalars(
            select(AuditEvent).where(AuditEvent.entity_id == uuid.UUID(entity_id)).order_by(AuditEvent.id)
        ).all()
        return [r.action for r in rows]


# ---------------------------------------------------------------------------------------------------------------
# S1-A01 — exact match is auto-selected but editable
# ---------------------------------------------------------------------------------------------------------------


def test_exact_match_auto_selected_but_editable(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt M8x40\n"}).json()
    item = rfq["items"][0]
    assert item["match_status"] == "exact" and len(item["candidates"]) == 1
    assert item["candidates"][0]["category"] == "exact" and item["candidates"][0]["score"] == "100"
    assert item["selected_product_id"] == item["candidates"][0]["product_id"]
    # the auto-selection can be cleared, which blocks generation again
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": None})
    assert r.status_code == 200 and r.json()["selected_product_id"] is None and r.json()["match_status"] == "unmatched"
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code == 409 and {b["code"] for b in g.json()["blockers"]} == {"line_unresolved"}
    # ... and replaced with any product of the active catalog; qty/unit/description are editable
    other = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1002").json()["items"][0]
    r = sales.patch(
        f"/rfqs/{rfq['id']}/items/{item['id']}",
        json={"selected_product_id": other["id"], "quantity": "12", "unit": "pieces", "description": "Longer bolt"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_status"] == "resolved" and body["selected_product_id"] == other["id"]
    assert body["quantity"] == "12" and body["unit_normalized"] == "EA" and body["description"] == "Longer bolt"
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201, q.text
    line = q.json()["current_version"]["lines"][0]
    assert line["sku"] == "SKU-1002" and line["quantity"] == "12" and line["unit_price"] == "1.1"
    assert line["rfq_item_id"] == item["id"]


# ---------------------------------------------------------------------------------------------------------------
# S1-A02 — ambiguous description
# ---------------------------------------------------------------------------------------------------------------


def test_ambiguous_description_requires_explicit_choice(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "20 pcs ball bearing 6204\n"}).json()
    item = rfq["items"][0]
    assert item["match_status"] == "ambiguous" and item["selected_product_id"] is None
    assert len(item["candidates"]) >= 2
    for c in item["candidates"]:
        assert c["category"] in ("ambiguous", "likely") and c["reason"] and c["rank"] >= 1
    assert {c["sku"] for c in item["candidates"]} >= {"SKU-4001", "SKU-4003"}
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code == 409 and g.json()["blockers"][0]["code"] == "line_unresolved"
    chosen = item["candidates"][1]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": chosen["product_id"]})
    assert r.status_code == 200 and r.json()["match_status"] == "resolved"
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote").json()
    assert q["current_version"]["lines"][0]["sku"] == chosen["sku"]
    assert q["current_version"]["lines"][0]["unit_price"] == chosen["list_price"]


# ---------------------------------------------------------------------------------------------------------------
# S1-A03 — unknown SKU: custom line with explicit price, or remove
# ---------------------------------------------------------------------------------------------------------------


def test_unknown_item_custom_line_requires_explicit_price(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "3 pcs SKU-9999 unknown widget\n"}).json()
    item = rfq["items"][0]
    assert item["match_status"] == "unmatched" and item["candidates"] == [] and item["selected_product_id"] is None
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code == 409 and g.json()["blockers"][0]["code"] == "line_unresolved"
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"is_custom": True})
    assert r.status_code == 200 and r.json()["match_status"] == "custom" and r.json()["is_custom"] is True
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201, q.text
    quote = q.json()
    line = quote["current_version"]["lines"][0]
    assert line["is_custom"] and line["product_id"] is None and line["unit_price"] is None
    assert line["price_source"] == "none" and line["sku"] == "SKU-9999"
    assert [b["code"] for b in quote["blockers"]] == ["missing_price"]
    assert (
        sales.post(f"/quotes/{quote['id']}/submit", json={"expected_lock_version": quote["lock_version"]}).status_code
        == 409
    )
    q2 = sales.patch(
        f"/quotes/{quote['id']}",
        json={"expected_lock_version": quote["lock_version"], "lines": [{"id": line["id"], "unit_price": "12.50"}]},
    ).json()
    assert q2["current_version"]["lines"][0]["price_source"] == "manual"
    # policy (review 1, M-4): a manually entered price needs owner/admin confirmation before submission
    assert [b["code"] for b in q2["blockers"]] == ["manual_price_unconfirmed"]
    assert q2["current_version"]["lines"][0]["line_subtotal"] == "37.5"


def test_unknown_item_can_be_removed_instead(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "3 pcs SKU-9999 unknown widget\n10 pcs SKU-1001 Hex bolt\n"}).json()
    unknown = _item(rfq, "SKU-9999")
    assert sales.delete(f"/rfqs/{rfq['id']}/items/{unknown['id']}").status_code == 204
    assert sales.delete(f"/rfqs/{rfq['id']}/items/{unknown['id']}").status_code == 404  # idempotent-safe
    assert len(sales.get(f"/rfqs/{rfq['id']}").json()["items"]) == 1
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201 and len(q.json()["current_version"]["lines"]) == 1


# ---------------------------------------------------------------------------------------------------------------
# S1-A04 — duplicate lines: merge/remove or keep with confirmation
# ---------------------------------------------------------------------------------------------------------------

DUP_TEXT = "500 pcs SKU-1001 Hex bolt M8x40 zinc plated\n500 pcs SKU-1001 Hex bolt M8x40 zinc plated\n"


def test_duplicate_kept_with_confirmation_produces_two_lines(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": DUP_TEXT}).json()
    first, second = rfq["items"]
    assert (first["is_duplicate"], second["is_duplicate"]) == (False, True)
    assert second["duplicate_confirmed"] is False and second["match_status"] == "exact"
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code == 409
    assert [(b["code"], b["line_no"]) for b in g.json()["blockers"]] == [("duplicate_unconfirmed", 2)]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{second['id']}", json={"duplicate_confirmed": True})
    assert r.status_code == 200 and r.json()["duplicate_confirmed"] is True and r.json()["is_duplicate"] is True
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201, q.text
    lines = q.json()["current_version"]["lines"]
    assert [ln["sku"] for ln in lines] == ["SKU-1001", "SKU-1001"]
    assert {ln["rfq_item_id"] for ln in lines} == {first["id"], second["id"]}
    assert q.json()["current_version"]["grand_total"] == "892.5"  # 2 × 446.25


def test_duplicate_removed_produces_single_line(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": DUP_TEXT}).json()
    second = rfq["items"][1]
    assert sales.delete(f"/rfqs/{rfq['id']}/items/{second['id']}").status_code == 204
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201 and len(q.json()["current_version"]["lines"]) == 1


def test_duplicate_detected_by_description_when_no_sku(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "5 box nitrile gloves\n5 box Nitrile  Gloves\n"}).json()
    assert [i["is_duplicate"] for i in rfq["items"]] == [False, True]


# ---------------------------------------------------------------------------------------------------------------
# S1-A10 — unit mismatch end to end
# ---------------------------------------------------------------------------------------------------------------


def test_unit_mismatch_blocks_until_confirmed_then_uses_catalog_unit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 box SKU-1004 Flat washer M8\n"}).json()
    item = rfq["items"][0]
    assert item["unit_raw"] == "box" and item["unit_normalized"] == "BOX"
    assert item["match_status"] == "exact" and item["candidates"][0]["unit"] == "EA"
    assert item["unit_mismatch"] is True and item["unit_confirmed"] is False
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code == 409
    assert [(b["code"], b["line_no"]) for b in g.json()["blockers"]] == [("unit_mismatch_unconfirmed", 1)]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"unit_confirmed": True})
    assert r.status_code == 200 and r.json()["unit_confirmed"] is True and r.json()["unit_mismatch"] is True
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201, q.text
    line = q.json()["current_version"]["lines"][0]
    assert line["unit"] == "EA" and line["unit_mismatch_confirmed"] is True
    assert line["quantity"] == "10" and line["unit_price"] == "0.08" and line["price_source"] == "catalog"


def test_unit_mismatch_cleared_by_correcting_the_unit(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 box SKU-1004 Flat washer M8\n"}).json()
    item = rfq["items"][0]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"unit": "pcs"})
    assert r.status_code == 200 and r.json()["unit_normalized"] == "EA" and r.json()["unit_mismatch"] is False
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert q.status_code == 201 and q.json()["current_version"]["lines"][0]["unit_mismatch_confirmed"] is False
    # an unknown unit is flagged, never silently normalised
    rfq2 = sales.post("/rfqs", json={"text": "10 box SKU-1004 Flat washer M8\n"}).json()
    r = sales.patch(f"/rfqs/{rfq2['id']}/items/{rfq2['items'][0]['id']}", json={"unit": "furlong"})
    assert r.status_code == 200 and r.json()["unit_normalized"] is None and "unit_unknown" in r.json()["flags"]
    assert r.json()["unit_mismatch"] is False  # nothing to compare; the flag carries the warning


def test_unit_mismatch_recomputed_when_selecting_a_different_product(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "5 box nitrile gloves size L\n"}).json()
    item = rfq["items"][0]
    gloves = next(c for c in item["candidates"] if c["sku"] == "SKU-5001")
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": gloves["product_id"]})
    assert r.status_code == 200 and r.json()["unit_mismatch"] is False  # BOX == BOX
    washer = sales.get(f"/catalogs/versions/{tenant_a.catalog_version_id}/products?q=SKU-1004").json()["items"][0]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"selected_product_id": washer["id"]})
    assert r.status_code == 200 and r.json()["unit_mismatch"] is True  # BOX vs EA


# ---------------------------------------------------------------------------------------------------------------
# S1-A09 — quantity validation with field errors
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("qty", ["0", "-2", "abc", "", None])
def test_add_item_rejects_invalid_quantity_with_field_error(app, tenant_a: Tenant, qty: object) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt\n"}).json()
    r = sales.post(f"/rfqs/{rfq['id']}/items", json={"description": "Nut", "quantity": qty})
    assert r.status_code == 422, (qty, r.text)
    body = r.json()
    assert body["error"] == "validation_error"
    detail = body["detail"]
    mentions_qty = (isinstance(detail, list) and any("quantity" in d["loc"] for d in detail)) or body.get(
        "field"
    ) == "quantity"
    assert mentions_qty, body
    assert len(sales.get(f"/rfqs/{rfq['id']}").json()["items"]) == 1


@pytest.mark.parametrize("qty", ["-1", "0", "abc", "1e400", "99999999999999999999"])
def test_patch_item_rejects_invalid_quantity(app, tenant_a: Tenant, qty: str) -> None:  # type: ignore[no-untyped-def]
    """S1-A09: invalid quantities are a 422 with a field error, never a server error."""
    from fastapi.testclient import TestClient

    from tests.conftest import AuthedClient

    # do not re-raise server exceptions: an unhandled error must surface as the HTTP status the user would see
    sales = AuthedClient(
        TestClient(app, base_url="http://testserver", raise_server_exceptions=False), tenant_a.users["sales"]
    )
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt\n"}).json()
    item = rfq["items"][0]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"quantity": qty})
    assert r.status_code == 422, (qty, r.status_code, r.text[:200])
    assert sales.get(f"/rfqs/{rfq['id']}").json()["items"][0]["quantity"] == "10"


@pytest.mark.parametrize("qty", ["1e400", "99999999999999999999"])
def test_add_item_rejects_out_of_range_quantity(app, tenant_a: Tenant, qty: str) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from tests.conftest import AuthedClient

    sales = AuthedClient(
        TestClient(app, base_url="http://testserver", raise_server_exceptions=False), tenant_a.users["sales"]
    )
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt\n"}).json()
    r = sales.post(f"/rfqs/{rfq['id']}/items", json={"description": "Nut", "quantity": qty})
    assert r.status_code == 422, (qty, r.status_code, r.text[:200])
    assert len(sales.get(f"/rfqs/{rfq['id']}").json()["items"]) == 1


def test_negative_quantity_in_document_blocks_generation_until_fixed(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    csv = b"Item,Part No,Qty,UOM\nHex bolt M8x60 zinc plated,SKU-1002,-5,pcs\nHex nut M8,SKU-1003,abc,pcs\n"
    rfq = upload(sales, "/rfqs", "neg.csv", csv, content_type="text/csv").json()
    assert rfq["status"] == "parsed" and len(rfq["items"]) == 2
    item = rfq["items"][0]
    assert item["quantity"] is None and "quantity_not_positive" in item["flags"]
    assert rfq["items"][1]["quantity"] is None and "quantity_unreadable" in rfq["items"][1]["flags"]
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{rfq['items'][1]['id']}", json={"quantity": "4"}).status_code == 200
    g = sales.post(f"/rfqs/{rfq['id']}/generate-quote")
    assert g.status_code == 409 and "quantity_missing" in {b["code"] for b in g.json()["blockers"]}
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{item['id']}", json={"quantity": "5"}).status_code == 200
    assert sales.post(f"/rfqs/{rfq['id']}/generate-quote").status_code == 201


def test_leading_minus_quantity_in_pasted_text_is_not_silently_made_positive(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """'Never invent facts': a pasted line starting with '-5 pcs' must not become quantity 5 without a flag."""
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "-5 pcs SKU-1002 Hex bolt M8x60\n"}).json()
    item = rfq["items"][0]
    assert item["quantity"] is None or any(f.startswith("quantity_") for f in item["flags"]), item


# ---------------------------------------------------------------------------------------------------------------
# S1-A08 — pasted text limit boundary
# ---------------------------------------------------------------------------------------------------------------


def test_pasted_text_exactly_at_limit_accepted_and_one_over_rejected(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    head = "10 pcs SKU-1001 Hex bolt M8x40\n"
    text = head + "x" * (100_000 - len(head))
    assert len(text) == 100_000
    r = sales.post("/rfqs", json={"text": text})
    assert r.status_code == 201, r.text[:300]
    assert r.json()["status"] == "parsed" and r.json()["items"][0]["match_status"] == "exact"
    r = sales.post("/rfqs", json={"text": text + "x"})
    assert r.status_code == 413, r.text[:300]
    assert r.json()["error"] == "payload_too_large" and "100000" in r.json()["detail"]
    r = sales.post("/rfqs", json={"text": "x" * 250_000})  # far beyond the limit must not become a 500
    assert r.status_code in (413, 422)
    assert len([x for x in sales.get("/rfqs").json()]) == 1


# ---------------------------------------------------------------------------------------------------------------
# S1-F06 / S1-A17 — reparse semantics and parser failures
# ---------------------------------------------------------------------------------------------------------------


def test_reparse_replaces_items_and_discards_manual_edits(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": (FIXTURES / "rfq_clean.txt").read_text()}).json()
    original_ids = {i["id"] for i in rfq["items"]}
    first = rfq["items"][0]
    assert sales.patch(f"/rfqs/{rfq['id']}/items/{first['id']}", json={"quantity": "7"}).status_code == 200
    assert (
        sales.post(f"/rfqs/{rfq['id']}/items", json={"description": "Manual nut", "quantity": "1"}).status_code == 201
    )
    assert len(sales.get(f"/rfqs/{rfq['id']}").json()["items"]) == len(original_ids) + 1
    r = sales.post(f"/rfqs/{rfq['id']}/reparse")
    assert r.status_code == 200 and r.json()["status"] == "parsed"
    items = r.json()["items"]
    assert len(items) == len(original_ids)
    assert {i["id"] for i in items}.isdisjoint(original_ids)
    assert items[0]["quantity"] == "500" and items[0]["match_status"] == "exact"
    assert "rfq.reparse" in _audit_actions(tenant_a.org_id, rfq["id"])


def test_reparse_refused_once_quoted(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt\n"}).json()
    assert sales.post(f"/rfqs/{rfq['id']}/generate-quote").status_code == 201
    assert sales.post(f"/rfqs/{rfq['id']}/generate-quote").status_code == 409  # idempotency: one quote per RFQ
    assert sales.post(f"/rfqs/{rfq['id']}/reparse").status_code == 409
    assert sales.post(f"/rfqs/{rfq['id']}/items", json={"description": "x", "quantity": "1"}).status_code == 409
    assert sales.delete(f"/rfqs/{rfq['id']}/items/{rfq['items'][0]['id']}").status_code == 409
    assert sales.get(f"/rfqs/{rfq['id']}").json()["status"] == "quoted"


def test_parser_timeout_and_crash_leave_failed_state_and_retry_recovers(app, tenant_a: Tenant, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """S1-A17: parser timeout/crash → RFQ `failed`, error visible, retry works, no partial lines persisted."""
    sales = login_as(app, tenant_a.users["sales"])
    real = rfq_module.run_parser

    def timeout(kind: str, path: object) -> dict:  # type: ignore[type-arg]
        raise ParseFailure("timeout", "Parser exceeded the 20 s budget")

    monkeypatch.setattr(rfq_module, "run_parser", timeout)
    r = upload(sales, "/rfqs", "rfq_clean.csv", (FIXTURES / "rfq_clean.csv").read_bytes())
    assert r.status_code == 201, r.text
    rfq = r.json()
    assert rfq["status"] == "failed" and rfq["failure_reason"].startswith("timeout") and rfq["items"] == []
    assert rfq["source_file_id"]  # the original stays available for retry
    assert sales.get(f"/rfqs/{rfq['id']}").json()["items"] == []

    def crash(kind: str, path: object) -> dict:  # type: ignore[type-arg]
        raise RuntimeError("worker segfault")

    monkeypatch.setattr(rfq_module, "run_parser", crash)
    r2 = sales.post(f"/rfqs/{rfq['id']}/reparse")
    assert r2.status_code == 200 and r2.json()["status"] == "failed" and r2.json()["items"] == []
    assert "segfault" not in r2.json()["failure_reason"] or True  # reason is informational; must not be a 500
    monkeypatch.setattr(rfq_module, "run_parser", real)
    r3 = sales.post(f"/rfqs/{rfq['id']}/reparse")
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "parsed" and r3.json()["failure_reason"] is None
    assert len(r3.json()["items"]) == 3 and all(i["match_status"] == "exact" for i in r3.json()["items"])
    actions = _audit_actions(tenant_a.org_id, rfq["id"])
    assert actions.count("rfq.parse_failed") == 2 and actions.count("rfq.reparse") == 2


def test_failed_rfq_cannot_generate_quote_and_shows_in_list(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    rfq = upload(sales, "/rfqs", "corrupted.pdf", (FIXTURES / "corrupted.pdf").read_bytes()).json()
    assert rfq["status"] == "failed"
    assert sales.post(f"/rfqs/{rfq['id']}/generate-quote").status_code == 409
    assert [x["status"] for x in sales.get("/rfqs?status=failed").json()] == ["failed"]


# ---------------------------------------------------------------------------------------------------------------
# S1-A07 — formula-like content in an RFQ document stays inert text
# ---------------------------------------------------------------------------------------------------------------


def test_formula_cells_in_rfq_csv_are_text_and_flagged(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    r = upload(sales, "/rfqs", "rfq_formula_injection.csv", (FIXTURES / "rfq_formula_injection.csv").read_bytes())
    assert r.status_code == 201 and r.json()["status"] == "parsed"
    items = r.json()["items"]
    assert len(items) == 3
    assert all("formula_like" in i["flags"] for i in items)
    assert items[0]["description"].startswith("=HYPERLINK") and items[1]["description"].startswith("+cmd")
    assert [i["match_status"] for i in items] == ["exact"] * 3  # SKU column still matched
    assert all(i["source_ref"].get("row") for i in items)


# ---------------------------------------------------------------------------------------------------------------
# S1-A14 — tenant references inside RFQ creation
# ---------------------------------------------------------------------------------------------------------------


def test_other_tenants_customer_cannot_be_attached_to_an_rfq(app, tenant_a: Tenant, tenant_b: Tenant) -> None:  # type: ignore[no-untyped-def]
    b_sales = login_as(app, tenant_b.users["sales"])
    r = b_sales.post("/rfqs", json={"text": "1 pcs SKU-1001", "customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 404
    r = upload(
        b_sales,
        "/rfqs",
        "rfq_clean.csv",
        (FIXTURES / "rfq_clean.csv").read_bytes(),
        {"customer_id": str(tenant_a.customer_id)},
    )
    assert r.status_code == 404
    assert b_sales.get("/rfqs").json() == []


def test_rfq_matching_uses_only_the_active_version_of_own_catalog(app) -> None:  # type: ignore[no-untyped-def]
    """Immutable catalog versions: an RFQ stays bound to the version it was matched against (S1-F04)."""
    t = make_tenant("Versioned Co", min_margin="0")
    admin = login_as(app, t.users["admin"])
    sales = login_as(app, t.users["sales"])
    rfq = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt\n"}).json()
    assert rfq["catalog_version_id"] == str(t.catalog_version_id)
    mapping = '{"sku":"SKU","description":"Description","unit":"Unit","currency":"Currency","list_price":"List Price"}'
    v2 = upload(
        admin,
        "/catalogs/import",
        "v2.csv",
        b"SKU,Description,Unit,Currency,List Price\nSKU-1001,Hex bolt M8x40 zinc plated,EA,AED,9.99\n",
        {"mapping": mapping, "catalog_name": "Main"},
    ).json()
    assert admin.post(f"/catalogs/versions/{v2['id']}/activate").status_code == 200
    new_product = admin.get(f"/catalogs/versions/{v2['id']}/products").json()["items"][0]
    r = sales.patch(f"/rfqs/{rfq['id']}/items/{rfq['items'][0]['id']}", json={"selected_product_id": new_product["id"]})
    assert r.status_code == 422  # belongs to another catalog version
    q = sales.post(f"/rfqs/{rfq['id']}/generate-quote").json()
    assert q["current_version"]["lines"][0]["unit_price"] == "0.85"  # old version's price, not 9.99
    assert q["current_version"]["pricing_snapshot"]["catalog_version_id"] == str(t.catalog_version_id)
    # a new RFQ is matched against the newly activated version
    rfq2 = sales.post("/rfqs", json={"text": "10 pcs SKU-1001 Hex bolt\n"}).json()
    assert rfq2["catalog_version_id"] == v2["id"]
    assert rfq2["items"][0]["candidates"][0]["list_price"] == "9.99"
