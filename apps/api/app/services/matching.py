"""Deterministic candidate matching (ADR-006): exact SKU → fuzzy description, categorical confidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from rapidfuzz import fuzz, process

from app.models.catalog import Product
from app.services.parsing.extract import SKU_TOKEN, looks_like_sku
from app.services.units import normalize_sku, normalize_text

LIKELY_MIN = 90.0
LIKELY_GAP = 10.0
AMBIGUOUS_MIN = 70.0
CUTOFF = 60.0
TOP_N = 5


@dataclass(frozen=True)
class Candidate:
    product: Product
    score: Decimal
    category: str  # exact | likely | ambiguous
    reason: str
    matched_on: str  # sku | sku_token | description


@dataclass(frozen=True)
class MatchOutcome:
    status: str  # exact | likely | ambiguous | unmatched
    candidates: tuple[Candidate, ...]


class CatalogIndex:
    def __init__(self, products: list[Product]) -> None:
        self.by_sku: dict[str, Product] = {}
        self.descriptions: list[str] = []
        self.products: list[Product] = []
        for p in products:
            self.by_sku.setdefault(p.sku_normalized, p)
            self.descriptions.append(normalize_text(f"{p.description} {p.sku}"))
            self.products.append(p)

    def match(self, *, sku_hint: str | None, description: str, raw_text: str) -> MatchOutcome:
        if sku_hint:
            p = self.by_sku.get(normalize_sku(sku_hint))
            if p is not None:
                return MatchOutcome(
                    "exact",
                    (Candidate(p, Decimal("100"), "exact", f"exact SKU match on '{sku_hint}'", "sku"),),
                )
        # tokens in the raw text that equal a catalog SKU
        for token in SKU_TOKEN.findall(raw_text or ""):
            if not looks_like_sku(token):
                continue
            p = self.by_sku.get(normalize_sku(token))
            if p is not None:
                return MatchOutcome(
                    "exact",
                    (
                        Candidate(
                            p,
                            Decimal("100"),
                            "exact",
                            f"SKU '{token}' found in request text",
                            "sku_token",
                        ),
                    ),
                )
        query = normalize_text(description or raw_text)
        if not query or not self.descriptions:
            return MatchOutcome("unmatched", ())
        hits = process.extract(query, self.descriptions, scorer=fuzz.WRatio, limit=TOP_N, score_cutoff=CUTOFF)
        if not hits:
            return MatchOutcome("unmatched", ())
        top = float(hits[0][1])
        second = float(hits[1][1]) if len(hits) > 1 else 0.0
        if top >= LIKELY_MIN and (top - second) >= LIKELY_GAP:
            status = "likely"
        elif top >= AMBIGUOUS_MIN:
            status = "ambiguous"
        else:
            status = "unmatched"
        cands: list[Candidate] = []
        for i, (_, score, idx) in enumerate(hits):
            p = self.products[int(idx)]
            cat = "likely" if (i == 0 and status == "likely") else "ambiguous"
            if status == "unmatched":
                cat = "ambiguous"
            overlap = _shared_terms(query, self.descriptions[int(idx)])
            reason = f"description similarity {float(score):.0f}%" + (
                f"; shared terms: {', '.join(overlap[:4])}" if overlap else ""
            )
            cands.append(Candidate(p, Decimal(f"{float(score):.2f}"), cat, reason, "description"))
        return MatchOutcome(status, tuple(cands))


def _shared_terms(a: str, b: str) -> list[str]:
    ta = {t for t in re.split(r"[^a-z0-9]+", a) if len(t) > 2}
    tb = {t for t in re.split(r"[^a-z0-9]+", b) if len(t) > 2}
    return sorted(ta & tb)
