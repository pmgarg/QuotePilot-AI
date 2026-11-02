"""JSON logging with request ids and a redaction filter. Never log bodies, documents or tokens."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|secret|token|cookie|authorization|api[_-]?key|set-cookie)", re.IGNORECASE
)
_SECRET_LIKE = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_\-]{20,}\.)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _SENSITIVE_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _EMAIL.sub("[EMAIL]", _SECRET_LIKE.sub("[REDACTED]", value))  # S2-F11: no addresses in logs
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, msg: str, **extra: Any) -> None:
    logger.info(msg, extra={"extra": extra})
