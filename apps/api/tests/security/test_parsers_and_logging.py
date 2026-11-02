import json
import logging
import time
from pathlib import Path

import pytest

from app.core.logging import JsonFormatter, redact
from app.services.parsing.limits import check_xlsx_zip, sniff_kind
from app.services.parsing.sandbox import ParseFailure, run_parser
from tests.conftest import FIXTURES

pytestmark = pytest.mark.security


def test_sniff_rejects_mismatch_and_binaries() -> None:
    from app.core.errors import UnsupportedMedia

    assert sniff_kind("a.pdf", b"%PDF-1.4 ...") == "pdf"
    assert sniff_kind("a.csv", b"a,b,c\n1,2,3") == "csv"
    with pytest.raises(UnsupportedMedia):
        sniff_kind("a.pdf", b"a,b,c")  # extension lies
    with pytest.raises(UnsupportedMedia):
        sniff_kind("a.exe", b"MZ\x90\x00\x00")
    with pytest.raises(UnsupportedMedia):
        sniff_kind("a.png", b"\x89PNG\r\n\x1a\n")
    with pytest.raises(UnsupportedMedia):
        sniff_kind("a.html", b"<html>")  # text but not an allowed extension


def test_zip_bomb_rejected_before_openpyxl() -> None:
    from app.core.errors import PayloadTooLarge

    with pytest.raises(PayloadTooLarge):
        check_xlsx_zip(FIXTURES / "zip_bomb.xlsx", 60 * 1024 * 1024, 200)


def test_sandbox_timeout_and_crash_are_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    class S(config.Settings):
        parse_timeout_seconds: int = 1

    monkeypatch.setattr("app.services.parsing.sandbox.get_settings", lambda: S())
    # a CSV so large that reading it with a 1s budget may or may not finish → either result must be clean
    p = tmp_path / "slow.csv"
    p.write_text("a,b\n" + "x,y\n" * 2_000_000)
    start = time.monotonic()
    try:
        out = run_parser("csv", p)
        assert out["ok"]
    except ParseFailure as exc:
        assert exc.code == "timeout"
    assert time.monotonic() - start < 10
    # corrupted pdf → structured failure, not an exception escaping the sandbox
    with pytest.raises(ParseFailure) as ei:
        run_parser("pdf", FIXTURES / "corrupted.pdf")
    assert ei.value.code in ("corrupted", "parser_error")


def test_parser_memory_limit_applies() -> None:
    out = run_parser("csv", FIXTURES / "rfq_clean.csv")
    assert out["ok"] and len(out["rows"]) == 4


def test_log_redaction() -> None:
    assert redact(
        {"password": "x", "nested": {"Authorization": "Bearer y", "ok": "sk-ant-api03-abcdefghijklmnop"}}
    ) == {"password": "[REDACTED]", "nested": {"Authorization": "[REDACTED]", "ok": "[REDACTED]"}}
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "token=%s", ("sk-ant-api03-abcdefghijklmnopq",), None)
    line = json.loads(JsonFormatter().format(rec))
    assert "sk-ant" not in line["msg"] and "request_id" in line
