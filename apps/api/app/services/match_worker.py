"""Fuzzy-matching subprocess entry point (SEC7-1). Run as ``python -I -m app.services.match_worker``.

Reads ONE JSON document from stdin::

    {"descriptions": [...], "queries": [...], "limit": N, "cutoff": X, "cpu_seconds": S, "memory_bytes": M}

applies resource limits (silently skipped where the platform refuses them) and, for each query index ``i`` in order,
writes one line ``{"i": i, "hits": [[score, choice_index], ...]}`` and flushes it. A parent that has to stop the
worker at its deadline therefore keeps every answer that was completed. Only the standard library and rapidfuzz are
imported; the scorer is the same one ``CatalogIndex`` uses in-process, so both paths return identical hits.
It never touches the database, the network or the environment.
"""

from __future__ import annotations

import json
import sys


def _apply_limits(mem_bytes: int, cpu_seconds: int) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except (ImportError, ValueError, OSError):
        pass


def main() -> int:
    try:
        doc = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        descriptions = [str(d) for d in doc["descriptions"]]
        queries = [str(q) for q in doc["queries"]]
        limit = int(doc["limit"])
        cutoff = float(doc["cutoff"])
        cpu_seconds = int(doc["cpu_seconds"])
        memory_bytes = int(doc["memory_bytes"])
    except (ValueError, KeyError, TypeError):
        return 2
    _apply_limits(memory_bytes, cpu_seconds)
    from rapidfuzz import fuzz, process

    out = sys.stdout
    for i, query in enumerate(queries):
        hits = process.extract(query, descriptions, scorer=fuzz.WRatio, limit=limit, score_cutoff=cutoff)
        out.write(json.dumps({"i": i, "hits": [[float(score), int(idx)] for _, score, idx in hits]}) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
