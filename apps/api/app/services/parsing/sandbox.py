"""Runs the parser worker in a separate, resource-limited process with a wall-clock timeout."""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - subprocess use is deliberate and argument-safe (no shell)
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings

MAX_OUTPUT_BYTES = 8 * 1024 * 1024


class ParseFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def run_parser(kind: str, path: Path) -> dict[str, Any]:
    settings = get_settings()
    cmd = [
        sys.executable,
        "-I",
        "-m",
        "app.services.parsing.worker",
        kind,
        str(path),
        str(settings.max_catalog_rows if kind in ("csv", "xlsx") else settings.max_rfq_rows),
        str(settings.max_pdf_pages),
        str(settings.parser_memory_limit_bytes),
        str(settings.parse_timeout_seconds),
    ]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
        "LANG": "C.UTF-8",
    }
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            timeout=settings.parse_timeout_seconds,
            env=env,
            cwd=str(Path(__file__).resolve().parents[3]),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ParseFailure("timeout", f"Parsing exceeded {settings.parse_timeout_seconds} seconds.") from exc
    if len(proc.stdout) > MAX_OUTPUT_BYTES:
        raise ParseFailure("too_large", "Parsed output exceeded the allowed size.")
    if proc.returncode != 0 and not proc.stdout:
        raise ParseFailure("parser_crash", "The document parser stopped unexpectedly.")
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseFailure("parser_crash", "The document parser returned unreadable output.") from exc
    if not isinstance(data, dict):
        raise ParseFailure("parser_crash", "The document parser returned unreadable output.")
    if not data.get("ok"):
        raise ParseFailure(str(data.get("error_code", "parser_error")), str(data.get("message", "Parsing failed.")))
    return data
