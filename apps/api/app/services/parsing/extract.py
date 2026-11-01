"""Turn parser output (rows / pages / pasted text) into RFQ ParsedItems. Pure Python, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.money import parse_decimal
from app.services.units import UNIT_ALIASES, normalize_unit

HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "quantity": (
        "qty",
        "quantity",
        "qnty",
        "q'ty",
        "qty.",
        "quant",
        "menge",
        "cantidad",
        "pcs",
        "nos",
        "no of units",
    ),
    "description": (
        "description",
        "desc",
        "item",
        "item description",
        "product",
        "material",
        "article",
        "name",
        "details",
        "specification",
        "spec",
    ),
    "sku": (
        "sku",
        "part",
        "part no",
        "part no.",
        "part number",
        "code",
        "item code",
        "product code",
        "article no",
        "ref",
        "reference",
        "model",
        "catalog no",
        "cat no",
        "mpn",
    ),
    "unit": ("unit", "uom", "units", "u/m", "measure", "einheit"),
}
SKU_TOKEN = re.compile(r"\b(?=[A-Za-z0-9\-_./]*\d)(?=[A-Za-z0-9\-_./]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9\-_./]{3,30}\b")
MEASUREMENT = re.compile(
    r"^\d+(?:[.,]\d+)?(?:mm|cm|m|km|kg|g|mg|ml|l|v|a|w|hz|ah|mah|bar|psi|in|ft|x\d+(?:mm|cm|m)?)$",
    re.IGNORECASE,
)
_UNIT_WORDS = sorted(UNIT_ALIASES.keys(), key=len, reverse=True)
_UNIT_RE = "|".join(re.escape(u) for u in _UNIT_WORDS)
ITEM_PREFIX = r"(?:[-•*·]\s*)?(?:(?:item|line|pos|sl|no|#)\.?\s*\d{1,3}\s*[:.)\-]\s*|\d{1,3}[.)]\s+)?"
QTY_LEAD = re.compile(
    rf"^\s*{ITEM_PREFIX}(?P<qty>-?\d+(?:[.,]\d+)?)\s*(?:x|×)?\s*(?P<unit>{_UNIT_RE})?\.?\s+(?P<desc>.+?)\s*$",
    re.IGNORECASE,
)
# a trailing quantity must be separated from the description by a separator, a keyword or whitespace
# (never glued to it, so "M8x60" is not read as quantity 60)
QTY_TRAIL = re.compile(
    rf"^\s*{ITEM_PREFIX}(?P<desc>.+?)"
    rf"(?:\s*[-–:,]\s*(?:qty\.?|quantity)?\s*[:=]?\s*|\s+(?:qty\.?|quantity|x|×)\s*[:=]?\s*|\s+)"
    rf"(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>{_UNIT_RE})?\.?\s*$",
    re.IGNORECASE,
)
NOISE = re.compile(
    r"^(?:total|subtotal|page \d+|thank you|regards|dear|please|kindly|request for quotation"
    r"|rfq|quotation|date|to:|from:|attn|subject)",
    re.IGNORECASE,
)


@dataclass
class ParsedItem:
    raw_text: str
    description: str
    quantity: Decimal | None = None
    unit_raw: str | None = None
    sku_hint: str | None = None
    source_ref: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def looks_like_sku(token: str) -> bool:
    """A SKU-like token mixes letters and digits, is not a plain measurement and has structure (separator or
    letter→digit transition), e.g. SKU-1001, AB123, 4711-X; but not 200mm, M8x40 or 12V."""
    t = token.strip()
    if len(t) < 4 or MEASUREMENT.match(t):
        return False
    if re.fullmatch(r"[A-Za-z]\d{1,2}(?:x\d+)?", t):
        return False  # thread sizes such as M8x40
    return bool(re.search(r"[\-_/.]", t) or re.match(r"^[A-Za-z]{2,}\d{2,}", t))


def _norm_header(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower().rstrip(":")


def detect_header(rows: list[list[Any]]) -> tuple[int | None, dict[str, int]]:
    """Find the header row among the first 15 rows; return (row index, column mapping)."""
    for idx, row in enumerate(rows[:15]):
        cells = [_norm_header(c) for c in row]
        mapping: dict[str, int] = {}
        for col, cell in enumerate(cells):
            if not cell:
                continue
            for field_name, syns in HEADER_SYNONYMS.items():
                if field_name not in mapping and (cell in syns or any(cell.startswith(s + " ") for s in syns)):
                    mapping[field_name] = col
                    break
        if "description" in mapping and ("quantity" in mapping or "sku" in mapping):
            return idx, mapping
    return None, {}


def _guess_columns(rows: list[list[Any]]) -> dict[str, int]:
    """No header: first mostly-numeric column → quantity, longest non-SKU text column → description,
    SKU-like column → sku."""
    width = max((len(r) for r in rows), default=0)
    numeric_score = [0] * width
    text_len = [0] * width
    text_count = [0] * width
    sku_score = [0] * width
    for r in rows[:200]:
        for i in range(width):
            v = r[i] if i < len(r) else None
            if v in (None, ""):
                continue
            d = parse_decimal(v)
            s = str(v)
            if d is not None and not re.search(r"[A-Za-z]", s):
                numeric_score[i] += 1
            else:
                text_len[i] += len(s)
                text_count[i] += 1
                if SKU_TOKEN.fullmatch(s.strip()) and looks_like_sku(s.strip()):
                    sku_score[i] += 1
    mapping: dict[str, int] = {}
    if width:
        prose = [i for i in range(width) if text_count[i] and sku_score[i] < text_count[i]]
        if prose:
            mapping["description"] = max(prose, key=lambda i: text_len[i])
        elif any(text_len):
            mapping["description"] = max(range(width), key=lambda i: text_len[i])
        nums = [i for i in range(width) if numeric_score[i] > 0 and i != mapping.get("description")]
        if nums:
            mapping["quantity"] = nums[0]
        skus = [i for i in range(width) if sku_score[i] > 0 and i not in mapping.values()]
        if skus:
            mapping["sku"] = max(skus, key=lambda i: sku_score[i])
    return mapping


def items_from_rows(rows: list[list[Any]], source: dict[str, Any]) -> list[ParsedItem]:
    header_idx, mapping = detect_header(rows)
    start = 0
    if header_idx is None:
        mapping = _guess_columns(rows)
    else:
        start = header_idx + 1
    items: list[ParsedItem] = []
    if "description" not in mapping:
        return items
    for ridx in range(start, len(rows)):
        row = rows[ridx]
        if not any(c not in (None, "") for c in row):
            continue

        def get(name: str, row: list[Any] = row) -> Any:
            col = mapping.get(name)
            return row[col] if col is not None and col < len(row) else None

        desc = str(get("description") or "").strip()
        sku = get("sku")
        qty_raw = get("quantity")
        if not desc and not sku:
            continue
        raw_text = " | ".join(str(c) for c in row if c not in (None, ""))[:2000]
        item = ParsedItem(raw_text=raw_text, description=desc[:1000], source_ref={**source, "row": ridx + 1})
        if sku not in (None, ""):
            item.sku_hint = str(sku).strip()[:100]
        qty = parse_decimal(qty_raw) if qty_raw not in (None, "") else None
        if qty_raw not in (None, "") and qty is None:
            item.flags.append("quantity_unreadable")
        if qty is not None and qty <= 0:
            item.flags.append("quantity_not_positive")
            qty = None
        item.quantity = qty
        unit = get("unit")
        if unit not in (None, ""):
            item.unit_raw = str(unit).strip()[:32]
        for cell in (desc, sku, unit):
            if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@", "\t", "\r"):
                if "formula_like" not in item.flags:
                    item.flags.append("formula_like")
        items.append(item)
    return items


def items_from_text_lines(text: str, source: dict[str, Any]) -> list[ParsedItem]:
    items: list[ParsedItem] = []
    for lidx, line in enumerate(text.splitlines()):
        s = line.strip()
        if len(s) < 3 or NOISE.match(s):
            continue
        item: ParsedItem | None = None
        m = QTY_LEAD.match(s)
        if m and len(m.group("desc")) >= 3:
            item = ParsedItem(
                raw_text=s[:2000],
                description=m.group("desc").strip()[:1000],
                quantity=parse_decimal(m.group("qty")),
                unit_raw=m.group("unit"),
            )
        else:
            m = QTY_TRAIL.match(s)
            if (
                m
                and len(m.group("desc")) >= 3
                and (
                    m.group("unit")
                    or re.search(r"(qty\.?|quantity|\sx|×|[-–:,])\s*[:=]?\s*\d+(?:[.,]\d+)?\s*$", s, re.IGNORECASE)
                )
            ):
                item = ParsedItem(
                    raw_text=s[:2000],
                    description=m.group("desc").strip(" -–:,")[:1000],
                    quantity=parse_decimal(m.group("qty")),
                    unit_raw=m.group("unit"),
                )
        if item is None:
            # keep lines that look like item descriptions with a SKU token but no quantity
            if SKU_TOKEN.search(s) and not re.search(r"@|www\.|http", s):
                item = ParsedItem(raw_text=s[:2000], description=s[:1000], flags=["quantity_missing"])
            else:
                continue
        if item.quantity is not None and item.quantity <= 0:
            item.flags.append("quantity_not_positive")
            item.quantity = None
        tokens = [t for t in SKU_TOKEN.findall(item.description) if looks_like_sku(t)]
        if tokens:
            item.sku_hint = tokens[0][:100]
        if item.unit_raw and normalize_unit(item.unit_raw) is None:
            item.flags.append("unit_unknown")
        item.source_ref = {**source, "line": lidx + 1}
        items.append(item)
    return items


def items_from_pdf(parsed: dict[str, Any]) -> tuple[list[ParsedItem], list[str]]:
    items: list[ParsedItem] = []
    flags: list[str] = []
    if not parsed.get("has_text"):
        return items, ["scanned_or_image_pdf"]
    for page in parsed.get("pages", []):
        page_no = page.get("page")
        table_items: list[ParsedItem] = []
        for tidx, table in enumerate(page.get("tables", [])):
            table_items.extend(items_from_rows(table, {"page": page_no, "table": tidx + 1}))
        if table_items:
            items.extend(table_items)
        else:
            items.extend(items_from_text_lines(page.get("text", ""), {"page": page_no}))
    if parsed.get("truncated"):
        flags.append("pages_truncated")
    return items, flags


def items_from_spreadsheet(parsed: dict[str, Any], kind: str) -> tuple[list[ParsedItem], list[str]]:
    items: list[ParsedItem] = []
    flags: list[str] = []
    if kind == "csv":
        items = items_from_rows(parsed.get("rows", []), {"sheet": "csv"})
    else:
        for sheet in parsed.get("sheets", []):
            items.extend(items_from_rows(sheet.get("rows", []), {"sheet": sheet.get("name")}))
    if parsed.get("truncated"):
        flags.append("rows_truncated")
    return items, flags
