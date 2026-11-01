"""Parser subprocess entry point. Runs with an isolated interpreter (-I), memory and CPU limits.

Usage: python -I -m app.services.parsing.worker <kind> <path> <max_rows> <max_pages> <mem_bytes> <cpu_seconds>
Writes one JSON document to stdout: {"ok": true, ...} or {"ok": false, "error_code": ..., "message": ...}.
It never evaluates formulas, macros, scripts or external references.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from typing import Any


def _apply_limits(mem_bytes: int, cpu_seconds: int) -> bool:
    """Apply resource limits; returns False when the platform refused them (reported to the parent)."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        return True
    except (ImportError, ValueError, OSError):
        return False


def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool | int | float):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)[:2000]


def parse_csv(path: str, max_rows: int) -> dict[str, Any]:
    with open(path, "rb") as fh:
        raw = fh.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return {"ok": False, "error_code": "undecodable", "message": "Could not decode CSV text."}
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows: list[list[Any]] = []
    truncated = False
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if i >= max_rows:
            truncated = True
            break
        rows.append([_cell(c) for c in row[:64]])
    return {"ok": True, "rows": rows, "truncated": truncated, "delimiter": delimiter}


def parse_xlsx(path: str, max_rows: int) -> dict[str, Any]:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
    except Exception as exc:  # noqa: BLE001 - any parser failure is reported generically
        return {
            "ok": False,
            "error_code": "corrupted",
            "message": f"Workbook could not be opened ({type(exc).__name__}).",
        }
    sheets: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for ws in wb.worksheets:
        rows: list[list[Any]] = []
        for row in ws.iter_rows(values_only=True):
            if total >= max_rows:
                truncated = True
                break
            cells = [_cell(c) for c in row[:64]]
            if any(c not in (None, "") for c in cells):
                rows.append(cells)
                total += 1
        sheets.append({"name": str(ws.title)[:100], "rows": rows})
        if truncated:
            break
    wb.close()
    return {"ok": True, "sheets": sheets, "truncated": truncated}


def parse_pdf(path: str, max_pages: int) -> dict[str, Any]:
    import pdfplumber

    pages: list[dict[str, Any]] = []
    truncated = False
    try:
        with pdfplumber.open(path) as pdf:
            for idx, page in enumerate(pdf.pages):
                if idx >= max_pages:
                    truncated = True
                    break
                text = page.extract_text() or ""
                tables: list[list[list[Any]]] = []
                try:
                    for table in page.extract_tables() or []:
                        tables.append([[_cell(c) for c in (row or [])[:32]] for row in table[:500]])
                except Exception:  # noqa: BLE001, S110 - table extraction is best effort; text fallback follows
                    pass
                pages.append({"page": idx + 1, "text": text[:200_000], "tables": tables})
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error_code": "corrupted",
            "message": f"PDF could not be read ({type(exc).__name__}).",
        }
    has_text = any(p["text"].strip() for p in pages)
    return {"ok": True, "pages": pages, "truncated": truncated, "has_text": has_text}


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        sys.stdout.write(json.dumps({"ok": False, "error_code": "usage", "message": "bad arguments"}))
        return 2
    kind, path, max_rows, max_pages, mem, cpu = (
        argv[1],
        argv[2],
        int(argv[3]),
        int(argv[4]),
        int(argv[5]),
        int(argv[6]),
    )
    limits_ok = _apply_limits(mem, cpu)
    try:
        if kind == "csv":
            out = parse_csv(path, max_rows)
        elif kind == "xlsx":
            out = parse_xlsx(path, max_rows)
        elif kind == "pdf":
            out = parse_pdf(path, max_pages)
        else:
            out = {"ok": False, "error_code": "unsupported", "message": "unsupported kind"}
    except MemoryError:
        out = {
            "ok": False,
            "error_code": "resource_limit",
            "message": "Document exceeded the memory limit.",
        }
    except Exception as exc:  # noqa: BLE001
        out = {
            "ok": False,
            "error_code": "parser_error",
            "message": f"Parser failed ({type(exc).__name__}).",
        }
    out["limits_applied"] = limits_ok
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
