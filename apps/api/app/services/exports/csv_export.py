"""Formula-injection-safe CSV writer (OWASP CSV injection)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence

DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralize(value: object) -> str:
    s = "" if value is None else str(value)
    if s[:1] in DANGEROUS_PREFIXES:
        return "'" + s
    return s


def write_csv(header: Sequence[str], rows: Iterable[Sequence[object]]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow([neutralize(h) for h in header])
    for row in rows:
        writer.writerow([neutralize(c) for c in row])
    return buf.getvalue().encode("utf-8-sig")
