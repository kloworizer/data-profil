"""Docling (IBM TableFormer) as a second PDF table reader.

TableFormer is a table-structure model: it recognises rows, columns, merged cells,
header cells and section rows from the page image, and fills the cells with the
PDF's own text (no OCR, so values are never re-typed). It is slower than the
geometry reader (seconds per page on CPU), so pdf.py only asks it about pages the
geometry reader is not sure about, and keeps whichever reading is better.

Optional: needs `pip install docling` and its models in AUTOEDA_DOCLING_MODELS
(`docling-tools models download layout tableformer -o models/docling`). Nothing is
downloaded at runtime.
"""
from __future__ import annotations

import hashlib
import os
import pickle
import tempfile
import threading
from pathlib import Path

from .. import config

_conv = None
_lock = threading.Lock()


def available() -> bool:
    if config.PDF_ENGINE == "plumber":
        return False
    try:
        import docling  # noqa: F401
    except ImportError:
        return False
    return Path(config.DOCLING_MODELS).is_dir()


def _converter():
    global _conv
    with _lock:
        if _conv is None:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")      # air-gapped: models come from the local folder
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
            from docling.document_converter import DocumentConverter, PdfFormatOption

            from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions

            o = PdfPipelineOptions(do_ocr=False, do_table_structure=True, artifacts_path=str(config.DOCLING_MODELS))
            o.accelerator_options = AcceleratorOptions(num_threads=max(1, config.DOCLING_THREADS),
                                                       device=AcceleratorDevice.CPU)
            o.table_structure_options.mode = (TableFormerMode.FAST if config.DOCLING_MODE == "fast"
                                              else TableFormerMode.ACCURATE)
            o.table_structure_options.do_cell_matching = True     # cell text from the PDF text layer
            _conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=o)})
        return _conv


def _file_key(path: Path) -> str:
    h = hashlib.sha1(config.DOCLING_MODE.encode())
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _one_page_pdf(path: Path, pi: int, tmp: str) -> str | None:
    """A PDF holding only page pi: the converter then parses one page instead of the whole file
    (a 19 MB report was re-read for every page). None when pypdf is missing or fails."""
    try:
        from pypdf import PdfReader, PdfWriter
        w = PdfWriter()
        w.add_page(PdfReader(str(path)).pages[pi])
        out = os.path.join(tmp, f"p{pi}.pdf")
        with open(out, "wb") as f:
            w.write(f)
        return out
    except Exception:
        return None


def page_raw(path, pi: int, key: str | None = None) -> list[dict]:
    """Tables on page pi as plain dicts (picklable, so a worker process can return them).
    Cached on disk by file content."""
    path = Path(path)
    cache = None
    if config.DOCLING_CACHE:
        try:
            key = key or _file_key(path)
            cache = config.DATA_DIR / "cache" / "docling" / key[:2] / f"{key}_{pi}.pkl"
            if cache.is_file():
                return pickle.loads(cache.read_bytes())
        except Exception:
            cache = None
    with tempfile.TemporaryDirectory() as tmp:
        one = _one_page_pdf(path, pi, tmp)
        src, rng = (one, (1, 1)) if one else (str(path), (pi + 1, pi + 1))
        res = _converter().convert(src, page_range=rng, raises_on_error=False)
        out = _extract(res)
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(pickle.dumps(out))
        except Exception:
            pass
    return out


def page_tables(path: Path, pi: int, make_table, raw: list[dict] | None = None) -> list:
    """Tables on page pi (0-based) as PageTable objects (built by make_table(grid,
    origin, cols, header_rows, sections)). `raw` = a reading prefetched by a worker."""
    if raw is None:
        raw = page_raw(path, pi)
    return [make_table(t["grid"], t["origin"], t["cols"], t["header_rows"], t["sections"]) for t in raw]


def _extract(res) -> list[dict]:
    doc = getattr(res, "document", None)
    out = []
    for tb in (doc.tables if doc is not None else []):
        d = tb.data
        if d.num_rows < 2 or d.num_cols < 2:
            continue
        grid: list[list] = [[""] * d.num_cols for _ in range(d.num_rows)]
        origin: dict = {}
        x0s: dict = {}
        x1s: dict = {}
        heads: set = set()
        sections: set = set()
        for cell in d.table_cells:
            r0, r1 = cell.start_row_offset_idx, cell.end_row_offset_idx
            c0, c1 = cell.start_col_offset_idx, cell.end_col_offset_idx
            if not (0 <= r0 < d.num_rows and 0 <= c0 < d.num_cols):
                continue
            grid[r0][c0] = cell.text or ""
            for r in range(r0, min(r1, d.num_rows)):
                for c in range(c0, min(c1, d.num_cols)):
                    if (r, c) != (r0, c0):
                        grid[r][c] = None
                        origin[(r, c)] = (r0, c0)
            if cell.column_header:
                heads.update(range(r0, r1))
            if cell.row_section:
                sections.add(r0)
            if cell.bbox is not None and c1 - c0 == 1:
                x0s.setdefault(c0, []).append(cell.bbox.l)
                x1s.setdefault(c0, []).append(cell.bbox.r)
        # header = the run of header-flagged rows at the top
        header_rows = 0
        while header_rows in heads:
            header_rows += 1
        out.append({"grid": grid, "origin": origin, "cols": _columns(x0s, x1s, d.num_cols, tb),
                    "header_rows": header_rows, "sections": sections})
    return out


def _columns(x0s: dict, x1s: dict, n: int, tb) -> list[tuple[float, float]]:
    """x-range per column from the cells that sit in exactly one column."""
    box = tb.prov[0].bbox if tb.prov else None
    lo, hi = (box.l, box.r) if box is not None else (0.0, float(n))
    cols = []
    for c in range(n):
        if c in x0s:
            cols.append((min(x0s[c]), max(x1s[c])))
        else:
            cols.append((lo + (hi - lo) * c / n, lo + (hi - lo) * (c + 1) / n))
    return cols
