"""Bound and redact text before it leaves the system (never costs, margins, credentials)."""

from __future__ import annotations

import re

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(password|passwd|api[_-]?key|secret)\s*[:=]\s*\S+"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card-like numbers
]


def redact_for_model(text: str, max_chars: int) -> str:
    out = text.replace("\x00", "")
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out[:max_chars]
