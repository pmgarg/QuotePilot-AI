"""Deterministic pricing engine (ADR-004). Pure functions over Decimal; no DB access."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.services.money import quantize

HUNDRED = Decimal("100")


@dataclass(frozen=True)
class LineInput:
    quantity: Decimal
    unit_price: Decimal | None
    discount_pct: Decimal = Decimal("0")
    tax_rate_pct: Decimal = Decimal("0")
    cost_price: Decimal | None = None


@dataclass(frozen=True)
class LineResult:
    line_subtotal: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal
    margin_pct: Decimal | None


@dataclass
class Totals:
    subtotal: Decimal = Decimal("0")
    discount_total: Decimal = Decimal("0")
    tax_total: Decimal = Decimal("0")
    grand_total: Decimal = Decimal("0")
    lines: list[LineResult] = field(default_factory=list)


def validate_line(line: LineInput) -> list[str]:
    errors: list[str] = []
    if line.quantity is None or line.quantity <= 0:
        errors.append("quantity must be greater than zero")
    if line.unit_price is not None and line.unit_price < 0:
        errors.append("unit price cannot be negative")
    if not (Decimal("0") <= line.discount_pct <= HUNDRED):
        errors.append("discount must be between 0 and 100")
    if not (Decimal("0") <= line.tax_rate_pct <= HUNDRED):
        errors.append("tax rate must be between 0 and 100")
    return errors


def compute_line(line: LineInput, currency: str, rounding_mode: str = "HALF_UP") -> LineResult:
    """Line-level rounding at every monetary step so totals are exact sums of displayed amounts."""
    price = line.unit_price if line.unit_price is not None else Decimal("0")
    subtotal = quantize(line.quantity * price, currency, rounding_mode)
    discount = quantize(subtotal * line.discount_pct / HUNDRED, currency, rounding_mode)
    net = subtotal - discount
    tax = quantize(net * line.tax_rate_pct / HUNDRED, currency, rounding_mode)
    total = net + tax
    margin: Decimal | None = None
    if line.cost_price is not None and line.unit_price is not None and line.unit_price > 0:
        effective_unit = line.unit_price * (HUNDRED - line.discount_pct) / HUNDRED
        if effective_unit > 0:
            margin = ((effective_unit - line.cost_price) / effective_unit * HUNDRED).quantize(Decimal("0.01"))
        else:
            margin = Decimal("-100.00")
    return LineResult(subtotal, discount, net, tax, total, margin)


def compute_totals(lines: list[LineInput], currency: str, rounding_mode: str = "HALF_UP") -> Totals:
    totals = Totals()
    for line in lines:
        r = compute_line(line, currency, rounding_mode)
        totals.lines.append(r)
        totals.subtotal += r.line_subtotal
        totals.discount_total += r.discount_amount
        totals.tax_total += r.tax_amount
        totals.grand_total += r.line_total
    return totals
