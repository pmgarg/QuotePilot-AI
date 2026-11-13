"""Validation of untrusted CSV / connector fields before they reach PostgreSQL (SEC3-04). Every helper raises
``FieldError`` with a short, safe message so one bad row or record is rejected on its own instead of failing the
whole batch with a database error."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.money import parse_decimal

MAX_QTY = Decimal("1000000000")  # NUMERIC(18,3)
MAX_PRICE = Decimal("1000000000")  # NUMERIC(18,6)
MAX_LINE_TOTAL = Decimal("100000000000000")  # NUMERIC(18,3) with headroom


class FieldError(ValueError):
    pass


def text_field(value: Any, *, name: str, max_len: int, required: bool = False, truncate: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise FieldError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise FieldError(f"{name} must be text")
    if "\x00" in value:
        raise FieldError(f"{name} contains a NUL byte")
    v = value.strip()
    if not v:
        if required:
            raise FieldError(f"{name} is required")
        return None
    if len(v) > max_len:
        if truncate:
            return v[:max_len]
        raise FieldError(f"{name} is longer than {max_len} characters")
    return v


def decimal_field(
    value: Any,
    *,
    name: str,
    minimum: Decimal | None = None,
    maximum: Decimal,
    exclusive_minimum: bool = False,
) -> Decimal:
    if isinstance(value, (dict, list, bool)):
        raise FieldError(f"{name} must be a number")
    d = parse_decimal(value)
    if d is None or not d.is_finite():
        raise FieldError(f"{name} must be a number")
    if minimum is not None and (d < minimum or (exclusive_minimum and d == minimum)):
        raise FieldError(f"{name} must be {'greater than' if exclusive_minimum else 'at least'} {minimum}")
    if d > maximum:
        raise FieldError(f"{name} is too large")
    return d


def order_line(raw: dict[str, Any], *, default_tax: Decimal) -> dict[str, Any]:
    """One validated order line from a CSV row or a connector record."""
    sku = text_field(raw.get("sku"), name="sku", max_len=100)
    description = text_field(raw.get("description"), name="description", max_len=2000)
    if sku is None and description is None:
        raise FieldError("sku or description required")
    qty = decimal_field(
        raw.get("quantity"), name="quantity", minimum=Decimal(0), exclusive_minimum=True, maximum=MAX_QTY
    )
    price = decimal_field(raw.get("unit_price"), name="unit_price", minimum=Decimal(0), maximum=MAX_PRICE)
    if qty * price > MAX_LINE_TOTAL:
        raise FieldError("line total is too large")
    tax_raw = raw.get("tax_rate_pct")
    if tax_raw is None or tax_raw == "":
        tax = default_tax
    else:
        try:
            tax = decimal_field(tax_raw, name="tax_rate_pct", minimum=Decimal(0), maximum=Decimal(100))
        except FieldError:
            raise FieldError("tax_rate_pct must be between 0 and 100") from None
    unit = text_field(raw.get("unit"), name="unit", max_len=16) or "EA"
    return {
        "sku": sku,
        "description": description or sku,
        "quantity": qty,
        "unit": unit,
        "unit_price": price,
        "tax_rate_pct": tax,
    }
