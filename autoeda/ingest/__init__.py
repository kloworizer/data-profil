"""Detect the file format (magic bytes first, extension second) and ingest it
into one or more string/typed Parquet tables."""
from __future__ import annotations

import re
from pathlib import Path

from ..models import Table

TEXT_EXT = {".csv", ".tsv", ".txt", ".dat", ".psv", ".tab", ".log", ".prn", ".gz", ".asc"}


def sniff_format(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(4096)
    ext = path.suffix.lower()
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PAR1"):
        return "parquet"
    if head.startswith(b"SQLite format 3"):
        return "sqlite"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE container: legacy .xls, or an encrypted OOXML workbook
        if ext in (".xlsx", ".xlsm", ".xlsb") or b"E\x00n\x00c\x00r\x00y\x00p\x00t\x00" in _ole_names(path):
            return "encrypted"
        return "spreadsheet"
    if head.startswith(b"ORC"):
        return "orc"
    if head.startswith(b"ARROW1") or ext in (".feather", ".arrow", ".ipc"):
        return "arrow"
    if head.startswith(b"PK\x03\x04"):
        if ext in (".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb", ".ods") or b"xl/" in head \
                or b"opendocument.spreadsheet" in head:
            return "spreadsheet"
        return "unsupported"
    if head.startswith(b"\x1f\x8b"):
        return "text"  # gzip: assume compressed delimited text
    if ext in (".json", ".jsonl", ".ndjson", ".geojson"):
        return "json"
    if ext == ".xml" or head.lstrip().startswith(b"<?xml"):
        return "xml"
    stripped = head.lstrip(b"\xef\xbb\xbf \r\n\t")
    if stripped[:1] in (b"{", b"[") and ext not in TEXT_EXT:
        return "json"
    nul = head.count(b"\x00")
    utf16_no_bom = nul >= 0.4 * len(head) and (head[1::2].count(b"\x00") >= 0.8 * len(head[1::2])
                                              or head[0::2].count(b"\x00") >= 0.8 * len(head[0::2]))
    if nul > 0.02 * len(head) and not utf16_no_bom and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "unsupported"
    return "text"   # stray NUL bytes are stripped while parsing
    return "text"


def _ole_names(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(64 * 1024)


def ingest(path: Path, work_dir: Path, progress=lambda p, m: None, max_rows: int = 0) -> tuple[list[Table], str]:
    from . import delimited, excel, others, pdf

    fmt = sniff_format(path)
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", path.stem)[:40] or "data"
    progress(0.03, f"Detected format: {fmt}")
    if fmt == "text":
        return delimited.ingest_delimited(path, work_dir, name, progress, max_rows=max_rows), fmt
    if fmt == "json":
        return others.ingest_json(path, work_dir, name, progress, max_rows=max_rows), fmt
    if fmt == "encrypted":
        raise ValueError("The workbook is password-protected / encrypted. Remove the password and retry.")
    handlers = {
        "spreadsheet": excel.ingest_excel, "pdf": pdf.ingest_pdf,
        "parquet": others.ingest_parquet, "sqlite": others.ingest_sqlite, "xml": others.ingest_xml,
        "arrow": others.ingest_arrow, "orc": others.ingest_arrow,
    }
    if fmt not in handlers:
        raise ValueError(f"Unsupported binary format ({path.suffix or 'no extension'}).")
    return handlers[fmt](path, work_dir, name, progress), fmt
