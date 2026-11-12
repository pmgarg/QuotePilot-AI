import hashlib
from decimal import Decimal as D

from app.services.ai.provider import DisabledProvider, parse_proposal
from app.services.ai.redaction import redact_for_model
from app.services.exports.csv_export import neutralize, write_csv
from app.services.exports.pdf import PdfLine, PdfQuote, render_quote_pdf
from app.services.parsing.extract import detect_header, items_from_rows, items_from_text_lines, looks_like_sku


def test_text_lines_extraction() -> None:
    items = items_from_text_lines(
        "1. 10 pcs SKU-1001 Hex bolt\nCable tie 200mm black x 500\nPos 5 - Bearing 6204-2RS, qty: 12\nThank you\n", {}
    )
    assert [i.quantity for i in items] == [D("10"), D("500"), D("12")]
    assert items[0].sku_hint == "SKU-1001" and items[0].unit_raw == "pcs"
    assert items[1].sku_hint is None  # 200mm is a measurement, not a SKU
    assert items[2].sku_hint == "6204-2RS"


def test_negative_quantity_flagged() -> None:
    items = items_from_rows([["Item", "Qty"], ["Bolt", "-5"], ["Nut", "abc"]], {})
    assert items[0].quantity is None and "quantity_not_positive" in items[0].flags
    assert items[1].quantity is None and "quantity_unreadable" in items[1].flags


def test_formula_like_cells_flagged_not_executed() -> None:
    items = items_from_rows([["Item", "Part No", "Qty"], ["=HYPERLINK(1)", "SKU-1", "1"], ["+cmd", "SKU-2", "2"]], {})
    assert all("formula_like" in i.flags for i in items)
    assert items[0].description == "=HYPERLINK(1)"  # kept as inert text


def test_header_detection_and_guessing() -> None:
    idx, mapping = detect_header([["junk"], ["Description", "SKU", "Quantity", "UOM"]])
    assert idx == 1 and mapping == {"description": 0, "sku": 1, "quantity": 2, "unit": 3}
    items = items_from_rows([["Hex bolt", "SKU-1001", 10], ["Nut", "SKU-1003", 5]], {})
    assert [i.sku_hint for i in items] == ["SKU-1001", "SKU-1003"]


def test_looks_like_sku() -> None:
    assert looks_like_sku("SKU-1001") and looks_like_sku("AB123") and looks_like_sku("6204-2RS")
    assert not looks_like_sku("200mm") and not looks_like_sku("M8x40") and not looks_like_sku("12V")


def test_csv_neutralisation() -> None:
    for bad in ("=1+1", "+1", "-1", "@SUM", "\t1", "\r1"):
        assert neutralize(bad).startswith("'")
    assert neutralize("ok") == "ok" and neutralize(None) == ""
    out = write_csv(["a"], [["=HYPERLINK(1)"]]).decode("utf-8-sig")
    assert '"\'=HYPERLINK(1)"' in out


def _quote() -> PdfQuote:
    return PdfQuote(
        "Org",
        "Org LLC",
        "Street 1\nCity",
        "#123456",
        "footer",
        "Q-2026-00001",
        1,
        "APPROVED",
        "2026-11-01",
        "2026-12-01",
        "Cust",
        "R1",
        "AED",
        (PdfLine(1, "SKU-1", "Desc <b>&", D("2"), "EA", D("1.5"), D("0"), D("5"), D("3.15")),),
        D("3.00"),
        D("0"),
        D("0.15"),
        D("3.15"),
        "notes",
        "terms",
    )


def test_pdf_deterministic_and_escaped() -> None:
    a, b = render_quote_pdf(_quote()), render_quote_pdf(_quote())
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
    assert a.startswith(b"%PDF-")


def test_ai_disabled_by_default_and_strict_parsing() -> None:
    assert DisabledProvider().propose_extraction("10 pcs bolt") is None
    assert parse_proposal("not json") is None
    assert (
        parse_proposal(
            '{"lines": [{"description": "x", "quantity": -1, "unit": null, "sku_hint": null, "source_excerpt": ""}], "notes": null}'
        )
        is None
    )
    ok = parse_proposal(
        '{"lines": [{"description": "bolt", "quantity": 2, "unit": "pcs", "sku_hint": "SKU-1", "source_excerpt": "2 pcs bolt"}], "notes": null}'
    )
    assert ok is not None and ok.lines[0].quantity == D("2")


def test_redaction_bounds_and_scrubs() -> None:
    out = redact_for_model("key sk-ant-api03-abcdefghijklmnop password: hunter2 " + "x" * 100, 60)
    assert "sk-ant" not in out and "hunter2" not in out and len(out) <= 60
