"""Pre-parse safety checks: type sniffing by magic bytes and XLSX zip-bomb guard."""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.core.errors import PayloadTooLarge, UnsupportedMedia, ValidationFailed

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"

ALLOWED_KINDS = {"pdf", "xlsx", "csv"}
EXTENSION_KIND = {
    ".pdf": "pdf",
    ".xlsx": "xlsx",
    ".xlsm": None,
    ".xls": None,
    ".csv": "csv",
    ".txt": "csv",
    ".tsv": "csv",
}


def sniff_kind(filename: str, head: bytes) -> str:
    """Determine the parser kind from magic bytes, cross-checked with the extension. Raises 415."""
    ext = Path(filename or "").suffix.lower()
    if head.startswith(PDF_MAGIC):
        kind = "pdf"
    elif head.startswith(ZIP_MAGIC):
        kind = "xlsx"
    else:
        try:
            head.decode("utf-8")
            kind = "csv"
        except UnicodeDecodeError:
            try:
                head.decode("latin-1")
                kind = "csv" if b"\x00" not in head else "binary"
            except UnicodeDecodeError:
                kind = "binary"
    if kind == "binary":
        raise UnsupportedMedia("Unsupported file type. Upload a digitally readable PDF, XLSX or CSV.")
    expected = EXTENSION_KIND.get(ext)
    if ext in EXTENSION_KIND and expected is None:
        raise UnsupportedMedia(f"File type '{ext}' is not supported (macro-enabled or legacy formats are rejected).")
    if expected is not None and expected != kind:
        raise UnsupportedMedia("File content does not match its extension.")
    if ext not in EXTENSION_KIND and kind == "csv":
        raise UnsupportedMedia("Unsupported file type. Upload a digitally readable PDF, XLSX or CSV.")
    return kind


def check_size(size: int, max_bytes: int) -> None:
    if size > max_bytes:
        raise PayloadTooLarge(f"File exceeds the maximum size of {max_bytes // (1024 * 1024)} MiB.")
    if size == 0:
        raise ValidationFailed("File is empty.")


def check_xlsx_zip(path: Path, max_uncompressed: int, max_ratio: int) -> None:
    """Reject zip bombs before openpyxl touches the file."""
    try:
        with zipfile.ZipFile(path) as zf:
            total = 0
            for info in zf.infolist():
                total += info.file_size
                if total > max_uncompressed:
                    raise PayloadTooLarge("Spreadsheet expands beyond the allowed size.")
                if (
                    info.compress_size > 0
                    and info.file_size / info.compress_size > max_ratio
                    and info.file_size > 1_000_000
                ):
                    raise PayloadTooLarge("Spreadsheet compression ratio is not allowed.")
            names = set(zf.namelist())
            if "[Content_Types].xml" not in names or not any(n.startswith("xl/") for n in names):
                raise UnsupportedMedia("File is not a valid XLSX workbook.")
    except zipfile.BadZipFile as exc:
        raise ValidationFailed("Spreadsheet file is corrupted or not a valid XLSX.") from exc
