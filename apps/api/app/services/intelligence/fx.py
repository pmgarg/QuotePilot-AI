"""Reporting-currency conversion (S3-F04 currency rule). Rates are maintained manually per tenant and are
*reporting* rates only — they never touch quote pricing. Missing rate ⇒ the amount lands in the
"unconverted" bucket for its currency instead of being guessed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intelligence import FxRate
from app.services.money import quantize


@dataclass
class Converter:
    reporting_currency: str
    rates: dict[tuple[str, str], list[tuple[date, Decimal]]]  # (base, quote) → [(as_of, rate)] sorted ascending
    unconverted: dict[str, Decimal] = field(default_factory=dict)
    unconverted_count: int = 0

    def rate_for(self, currency: str, on: date) -> Decimal | None:
        if currency == self.reporting_currency:
            return Decimal(1)
        direct = self._latest((currency, self.reporting_currency), on)
        if direct is not None:
            return direct
        inverse = self._latest((self.reporting_currency, currency), on)
        if inverse is not None and inverse != 0:
            return Decimal(1) / inverse
        return None

    def _latest(self, pair: tuple[str, str], on: date) -> Decimal | None:
        best: Decimal | None = None
        for as_of, rate in self.rates.get(pair, []):
            if as_of <= on:
                best = rate
            else:
                break
        return best

    def convert(self, amount: Decimal, currency: str, on: date) -> Decimal | None:
        """Converted + quantised amount, or None (and recorded as unconverted) when no rate exists."""
        rate = self.rate_for(currency, on)
        if rate is None:
            self.unconverted[currency] = self.unconverted.get(currency, Decimal("0")) + amount
            self.unconverted_count += 1
            return None
        return quantize(amount * rate, self.reporting_currency)


def load_converter(db: Session, org_id: uuid.UUID, reporting_currency: str) -> Converter:
    rows = db.scalars(select(FxRate).where(FxRate.organization_id == org_id).order_by(FxRate.as_of)).all()
    rates: dict[tuple[str, str], list[tuple[date, Decimal]]] = {}
    for r in rows:
        rates.setdefault((r.base_currency, r.quote_currency), []).append((r.as_of, Decimal(r.rate)))
    return Converter(reporting_currency=reporting_currency, rates=rates)
