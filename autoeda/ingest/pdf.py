"""PDF ingestion.

1. Ruled tables (lattice) per page; borderless tables via text alignment when a
   page has no ruled table but looks tabular.
2. Merged cells (pdfplumber returns None for spanned cells) are filled,
   phantom empty columns dropped, ligatures / full-width chars normalised.
3. Tables continuing on the next page are stitched (repeated header removed).
4. Wrapped records (one row printed over several lines) are re-joined.
5. No tables at all -> text layer, minus repeated page headers/footers and page
   numbers, parsed as delimited / fixed-width text. Scanned PDFs -> OCR when
   pytesseract + Tesseract are installed.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from ..models import RepairLog
from .delimited import ingest_delimited
from .grid import cell_str, grid_to_table, norm_text
from .repair import vclass

TEXT_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text", "snap_tolerance": 3,
                 "join_tolerance": 3, "min_words_vertical": 3, "min_words_horizontal": 1}
PAGE_NO_RE = re.compile(r"^\s*[-–]?\s*(page|hal(aman)?|halaman|p\.|pg\.?|seite)?\s*\d+\s*((of|dari|von|/)\s*\d+)?\s*[-–]?\s*$", re.I)


def _header_rows(rows) -> int:
    """Number of leading header rows (label-only rows before the first data row)."""
    k = 0
    for r in rows[:3]:
        cells = [cell_str(c) for c in r if c not in (None, "")]
        if cells and all(vclass(c) == "T" for c in cells):
            k += 1
        else:
            break
    return min(k, len(rows) - 1)


def _clean_table(raw: list, stats: Counter) -> tuple[list[list[str]], int] | None:
    """Returns (rows, number of header rows) or None."""
    rows = [list(r) for r in raw if r]
    if len(rows) < 2:
        return None
    w = max(len(r) for r in rows)
    rows = [r + [None] * (w - len(r)) for r in rows]
    nh = max(1, _header_rows(rows))
    # header rows: a None cell continues the merged cell on its left (only on the top
    # row: lower header rows stay empty under a vertically merged label)
    for c in range(1, w):
        if rows[0][c] is None and rows[0][c - 1] is not None:
            rows[0][c] = rows[0][c - 1]
            stats["merged_cells_filled"] += 1
    for r in range(1, nh):
        rows[r] = ["" if v is None else v for v in rows[r]]
    # body: None right after a value in the same row = horizontal span (leave empty);
    # otherwise = vertical span -> copy the value from above
    for r in range(nh, len(rows)):
        for c in range(w):
            if rows[r][c] is None:
                if c > 0 and rows[r][c - 1] not in (None, ""):
                    rows[r][c] = ""
                elif rows[r - 1][c] not in (None, ""):
                    rows[r][c] = rows[r - 1][c]
                    stats["merged_cells_filled"] += 1
    rows = [[norm_text(cell_str(v)).replace("\n", " ").strip() for v in r] for r in rows]
    keep = [c for c in range(w) if any(r[c] for r in rows)]
    if len(keep) < w:
        stats["empty_column_dropped"] += w - len(keep)
    rows = [[r[c] for c in keep] for r in rows]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2 or len(keep) < 2:
        return None
    return rows, nh


def _looks_tabular(rows: list[list[str]]) -> bool:
    """Reject text-strategy 'tables' that are really prose."""
    if len(rows) < 3 or len(rows[0]) < 2:
        return False
    cells = [c for r in rows for c in r]
    filled = [c for c in cells if c]
    if len(filled) < 0.4 * len(cells):
        return False
    avg_len = sum(len(c) for c in filled) / len(filled)
    numeric = sum(1 for c in filled if re.search(r"\d", c)) / len(filled)
    return avg_len < 40 and numeric >= 0.15


def _ocr_page(page) -> str:
    import pytesseract
    img = page.to_image(resolution=300).original
    return pytesseract.image_to_string(img, lang=os.getenv("AUTOEDA_OCR_LANG", "eng"))


def ingest_pdf(path: Path, work_dir: Path, name: str, progress=lambda p, m: None):
    import pdfplumber

    groups: list[dict] = []
    page_lines: list[list[str]] = []
    stats = Counter()
    ocr_pages = 0
    try:
        pdf = pdfplumber.open(str(path))
    except Exception as e:
        if "password" in str(e).lower() or "encrypt" in type(e).__name__.lower() or "Password" in type(e).__name__:
            raise ValueError("PDF is password-protected; remove the password and retry.")
        raise ValueError(f"Could not open PDF ({type(e).__name__}: {str(e)[:200]}).")
    with pdf:
        n_pages = len(pdf.pages)
        for pi, page in enumerate(pdf.pages):
            if pi % 5 == 0:
                progress(0.05 + 0.4 * pi / max(1, n_pages), f"Reading PDF page {pi + 1}/{n_pages}")
            found = [t for t in (_clean_table(t, stats) for t in page.extract_tables() or []) if t]
            if not found:
                found = [t for t in (_clean_table(t, stats) for t in page.extract_tables(TEXT_SETTINGS) or [])
                         if t and _looks_tabular(t[0])]
                if found:
                    stats["borderless_tables"] += len(found)
            for rows, nh in found:
                w = len(rows[0])
                last = groups[-1] if groups else None
                if last and last["width"] == w and last["last_page"] in (pi, pi - 1):
                    # continuation: drop the header block repeated at the top of the page
                    lower = lambda rs: [[c.lower() for c in r] for r in rs]  # noqa: E731
                    k = len(last["header"])
                    body = rows[k:] if lower(rows[:k]) == lower(last["header"]) else rows
                    if body is not rows:
                        stats["repeated_header"] += k
                    last["rows"].extend(body)
                    last["last_page"] = pi
                else:
                    groups.append({"width": w, "header": rows[:nh], "rows": rows, "first_page": pi + 1,
                                   "last_page": pi})
            text = page.extract_text() or ""
            if len(text.strip()) < 20 and not found:
                try:
                    text = _ocr_page(page)
                    ocr_pages += 1
                except Exception:
                    pass
            page_lines.append([norm_text(l) for l in text.splitlines()])
            page.flush_cache()

    tables = []
    for gi, g in enumerate(groups):
        log = RepairLog()
        log.note("Table extracted from PDF pages {a}–{b}.", a=g["first_page"], b=g["last_page"] + 1)
        for k in ("merged_cells_filled", "empty_column_dropped", "repeated_header"):
            if stats[k] and gi == 0:
                log.counts[k] += stats[k]
        if stats["borderless_tables"] and gi == 0:
            log.note("{n} borderless table(s) detected from text alignment.", n=stats["borderless_tables"])
        t = grid_to_table(g["rows"], work_dir / f"{name}_t{gi}.parquet", f"table_{gi + 1} (p.{g['first_page']})",
                          log, {"format": "pdf", "pages": f"{g['first_page']}-{g['last_page'] + 1}"},
                          wrapped_rows=True)
        if t is not None and t.n_rows:
            tables.append(t)
    if tables:
        return tables

    # ---- text layer fallback --------------------------------------------------------
    if not any(any(l.strip() for l in p) for p in page_lines):
        raise ValueError("PDF has no extractable text (scanned images?). Install Tesseract + pytesseract "
                         "(Docker build arg WITH_OCR=1) to enable OCR.")
    log = RepairLog()
    if ocr_pages:
        log.counts["ocr_page"] = ocr_pages
        log.note("{n} scanned page(s) read with OCR; expect recognition errors.", n=ocr_pages)
    # repeated page headers/footers: same line (digits masked) on >=50% of pages
    removed = 0
    if len(page_lines) >= 3:
        freq = Counter()
        for p in page_lines:
            freq.update({re.sub(r"\d+", "#", l.strip()) for l in p if l.strip()})
        furniture = {k for k, v in freq.items() if v >= 0.5 * len(page_lines)}
    else:
        furniture = set()
    text_path = work_dir / f"{name}_text.txt"
    with open(text_path, "w", encoding="utf-8") as f:
        for p in page_lines:
            for l in p:
                s = l.strip()
                if not s:
                    continue
                if PAGE_NO_RE.match(s) or re.sub(r"\d+", "#", s) in furniture:
                    removed += 1
                    continue
                f.write(l + "\n")
    progress(0.3, "No tables found: parsing PDF text layer")
    tables = ingest_delimited(text_path, work_dir, f"{name}_text", progress)
    for t in tables:
        t.name = "pdf_text"
        t.source = {"format": "pdf-text", "pages": len(page_lines)}
        if removed:
            t.repairs.counts["page_furniture_removed"] += removed
        t.repairs.counts.update(log.counts)
        for n in log.notes:
            t.repairs.note(n)
        t.repairs.note("No tables found in the PDF; the text layer was parsed as delimited / fixed-width text.")
    return tables
