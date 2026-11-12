"""Deterministic candidate matching (ADR-006): exact SKU → fuzzy description, categorical confidence."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # noqa: S404 - subprocess use is deliberate and argument-safe (no shell)
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from rapidfuzz import fuzz, process

from app.core.config import get_settings
from app.models.catalog import Product
from app.services.parsing.extract import looks_like_sku, sku_tokens
from app.services.units import normalize_sku, normalize_text

LIKELY_MIN = 90.0
LIKELY_GAP = 10.0
AMBIGUOUS_MIN = 70.0
CUTOFF = 60.0
TOP_N = 5
# Fuzzy matching compares one request line with every catalog description. Everything that can grow is bounded
# (SEC6-2, SEC7-1): the part of a line and of a catalog description that is compared, the total number of comparisons
# one index (= one RFQ parse or one re-match) may spend, and the wall-clock time. Small jobs run in the API process;
# larger ones run in a resource-limited worker process (`match_worker`) with a deadline, at most
# MAX_CONCURRENT_MATCHERS per API process, so the API keeps serving other requests. Lines that were not matched
# (budget, deadline, busy, worker failure) are still matched by exact SKU and are otherwise left for a person (flag
# `match_skipped_budget`); nothing is guessed.
MAX_QUERY_CHARS = 200
MAX_DESCRIPTION_CHARS = 200
MAX_FUZZY_COMPARISONS = 2_000_000
IN_PROCESS_MAX_COMPARISONS = 50_000
MAX_CONCURRENT_MATCHERS = 2
logger = logging.getLogger(__name__)
_matcher_slots = threading.BoundedSemaphore(MAX_CONCURRENT_MATCHERS)
Hits = list[tuple[float, int]]  # (score, index into the descriptions), best first


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
    fuzzy_skipped: bool = False  # the comparison budget of this index was exhausted before this line


def _fuzzy_in_process(descriptions: list[str], queries: list[str]) -> dict[int, Hits]:
    return {
        i: [
            (float(score), int(idx))
            for _, score, idx in process.extract(q, descriptions, scorer=fuzz.WRatio, limit=TOP_N, score_cutoff=CUTOFF)
        ]
        for i, q in enumerate(queries)
    }


def _parse_worker_lines(raw: bytes | str | None, n_queries: int, n_descriptions: int) -> dict[int, Hits]:
    """Every complete, well-formed line of the worker's output; a torn last line, garbage and out-of-range values
    are dropped (the worker answers queries in order, so what was parsed is a prefix)."""
    out: dict[int, Hits] = {}
    if not raw:
        return out
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    lines = text.split("\n")
    for line in lines[:-1]:  # the last element has no newline: incomplete or empty
        try:
            doc = json.loads(line)
            i = doc["i"]
            hits = [(float(score), int(idx)) for score, idx in doc["hits"]]
        except (ValueError, KeyError, TypeError):
            break
        if i != len(out) or not 0 <= i < n_queries or any(not 0 <= idx < n_descriptions for _, idx in hits):
            break
        out[i] = hits
    return out


def _fuzzy_in_worker(descriptions: list[str], queries: list[str]) -> dict[int, Hits]:
    settings = get_settings()
    timeout = float(settings.match_timeout_seconds)
    deadline = time.monotonic() + timeout
    payload = json.dumps(
        {
            "descriptions": descriptions,
            "queries": queries,
            "limit": TOP_N,
            "cutoff": CUTOFF,
            "cpu_seconds": int(timeout) + 2,
            "memory_bytes": settings.parser_memory_limit_bytes,
        }
    ).encode("utf-8")
    api_dir = Path(__file__).resolve().parents[2]
    cmd = [sys.executable, "-I", "-m", "app.services.match_worker"]
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(api_dir), "LANG": "C.UTF-8"}
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    if not _matcher_slots.acquire(timeout=timeout):
        logger.warning("matching skipped: all matcher processes are busy")
        return {}
    raw: bytes | None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {}
        try:
            proc = subprocess.run(  # noqa: S603
                cmd, input=payload, capture_output=True, timeout=remaining, env=env, cwd=str(api_dir), check=False
            )
            raw = proc.stdout
            if proc.returncode != 0:
                logger.warning("match worker exited with status %s", proc.returncode)
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout if isinstance(exc.stdout, bytes) else None
            logger.warning("match worker stopped at its %.1f s deadline", timeout)
        except OSError:
            logger.warning("match worker could not be started")
            return {}
    finally:
        _matcher_slots.release()
    return _parse_worker_lines(raw, len(queries), len(descriptions))


def fuzzy_batch(descriptions: list[str], queries: list[str]) -> dict[int, Hits]:
    """Top hits per query index. Small jobs run in-process; larger ones in a worker process with a deadline, and the
    result then holds only the queries that were answered in time (a prefix of ``queries``)."""
    if not queries or not descriptions:
        return {i: [] for i in range(len(queries))}
    if len(descriptions) * len(queries) <= IN_PROCESS_MAX_COMPARISONS:
        return _fuzzy_in_process(descriptions, queries)
    return _fuzzy_in_worker(descriptions, queries)


class CatalogIndex:
    def __init__(self, products: list[Product]) -> None:
        self.by_sku: dict[str, Product] = {}
        self.descriptions: list[str] = []
        self.products: list[Product] = []
        for p in products:
            self.by_sku.setdefault(p.sku_normalized, p)
            text = normalize_text((p.description or "")[: 4 * MAX_DESCRIPTION_CHARS])[:MAX_DESCRIPTION_CHARS]
            self.descriptions.append(normalize_text(f"{text} {p.sku}"))
            self.products.append(p)
        self.fuzzy_comparisons = 0
        self._requested: set[str] = set()  # queries sent by prefetch()
        self._hits: dict[str, Hits] = {}  # queries that were answered

    def _exact(self, sku_hint: str | None, raw_text: str) -> MatchOutcome | None:
        if sku_hint:
            p = self.by_sku.get(normalize_sku(sku_hint))
            if p is not None:
                return MatchOutcome(
                    "exact",
                    (Candidate(p, Decimal("100"), "exact", f"exact SKU match on '{sku_hint}'", "sku"),),
                )
        # tokens in the raw text that equal a catalog SKU
        for token in sku_tokens(raw_text or ""):
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
        return None

    @staticmethod
    def _query(description: str, raw_text: str) -> str:
        return normalize_text((description or raw_text)[: 4 * MAX_QUERY_CHARS])[:MAX_QUERY_CHARS]

    def prefetch(self, lines: Iterable[tuple[str | None, str, str]]) -> None:
        """Fuzzy-match all lines of one request (``(sku_hint, description, raw_text)`` in order) in ONE batch, so that
        a large job is run by the bounded worker process and ``match`` finds the answers. The comparison budget is
        applied in line order; lines beyond it, and lines the worker did not answer in time, are never matched."""
        queries: list[str] = []
        seen: set[str] = set()
        spent = self.fuzzy_comparisons
        for sku_hint, description, raw_text in lines:
            if self._exact(sku_hint, raw_text) is not None:
                continue
            query = self._query(description, raw_text)
            if not query or not self.descriptions:
                continue
            if spent + len(self.descriptions) > MAX_FUZZY_COMPARISONS:
                break  # match() applies the same rule to the same line and flags it
            spent += len(self.descriptions)
            if query not in seen:
                seen.add(query)
                queries.append(query)
        self._requested.update(queries)
        for i, hits in fuzzy_batch(self.descriptions, queries).items():
            self._hits[queries[i]] = hits

    def match(self, *, sku_hint: str | None, description: str, raw_text: str) -> MatchOutcome:
        exact = self._exact(sku_hint, raw_text)
        if exact is not None:
            return exact
        query = self._query(description, raw_text)
        if not query or not self.descriptions:
            return MatchOutcome("unmatched", ())
        if self.fuzzy_comparisons + len(self.descriptions) > MAX_FUZZY_COMPARISONS:
            return MatchOutcome("unmatched", (), fuzzy_skipped=True)
        self.fuzzy_comparisons += len(self.descriptions)
        if query in self._hits:
            hits = self._hits[query]
        elif query in self._requested:
            return MatchOutcome("unmatched", (), fuzzy_skipped=True)  # the deadline or a worker failure cut the batch
        else:
            answered = fuzzy_batch(self.descriptions, [query]).get(0)
            if answered is None:
                return MatchOutcome("unmatched", (), fuzzy_skipped=True)
            hits = answered
        if not hits:
            return MatchOutcome("unmatched", ())
        top = hits[0][0]
        second = hits[1][0] if len(hits) > 1 else 0.0
        if top >= LIKELY_MIN and (top - second) >= LIKELY_GAP:
            status = "likely"
        elif top >= AMBIGUOUS_MIN:
            status = "ambiguous"
        else:
            status = "unmatched"
        cands: list[Candidate] = []
        for i, (score, idx) in enumerate(hits):
            p = self.products[idx]
            cat = "likely" if (i == 0 and status == "likely") else "ambiguous"
            if status == "unmatched":
                cat = "ambiguous"
            overlap = _shared_terms(query, self.descriptions[idx])
            reason = f"description similarity {float(score):.0f}%" + (
                f"; shared terms: {', '.join(overlap[:4])}" if overlap else ""
            )
            cands.append(Candidate(p, Decimal(f"{float(score):.2f}"), cat, reason, "description"))
        return MatchOutcome(status, tuple(cands))


def _shared_terms(a: str, b: str) -> list[str]:
    ta = {t for t in re.split(r"[^a-z0-9]+", a) if len(t) > 2}
    tb = {t for t in re.split(r"[^a-z0-9]+", b) if len(t) > 2}
    return sorted(ta & tb)
