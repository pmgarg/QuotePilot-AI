"""Per-user sliding-window limits for the expensive write paths (SEC7-7). In-process, like the login limiter: with
several API replicas each one counts separately (see KNOWN_LIMITATIONS)."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque

from app.core.config import get_settings
from app.core.errors import RateLimited


class ActionRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, action: str, user_id: uuid.UUID, *, attempts: int, window_seconds: int) -> None:
        """Count one use of ``action`` by ``user_id``; raise RateLimited (HTTP 429, with ``retry_after``) when the
        user has already used it ``attempts`` times within the window. A refused call is not counted."""
        now = time.monotonic()
        key = f"{action}|{user_id}"
        with self._lock:
            if len(self._hits) > 10_000:  # forget users whose window has passed
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > window_seconds]:
                    del self._hits[k]
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= attempts:
                retry = max(1, math.ceil(window_seconds - (now - q[0])))
                raise RateLimited(
                    "Too many requests for this action; try again later", retry_after=retry, action=action
                )
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


action_limiter = ActionRateLimiter()


def limit_rfq_parse(user_id: uuid.UUID) -> None:
    s = get_settings()
    action_limiter.acquire(
        "rfq_parse",
        user_id,
        attempts=s.rfq_parse_rate_limit_attempts,
        window_seconds=s.expensive_rate_limit_window_seconds,
    )


def limit_catalog_import(user_id: uuid.UUID) -> None:
    s = get_settings()
    action_limiter.acquire(
        "catalog_import",
        user_id,
        attempts=s.catalog_import_rate_limit_attempts,
        window_seconds=s.expensive_rate_limit_window_seconds,
    )
