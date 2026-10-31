"""Spreadsheet ingestion (.xlsx .xlsm .xlsb .xls .ods) via python-calamine (Rust,
fast, low memory). One or more tables per sheet."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from ..models import RepairLog
from .grid import grid_to_table, grid_to_tables

# Sheets up to this many cells are materialised: enables multi-table detection
# and exact empty-column removal. Larger sheets are streamed row by row.
MATERIALIZE_MAX_CELLS = 5_000_000
FORMULA_SCAN_MAX_BYTES = 40 * 1024 * 1024


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:40] or "sheet"


def _filled_rows(rows, merges, r_off: int, c_off: int):
    """Yield rows with merged ranges filled by their top-left value (streaming)."""
    starts = defaultdict(list)
    for (r1, c1), (r2, c2) in merges:
        starts[r1 - r_off].append((r2 - r_off, c1 - c_off, c2 - c_off))
    active = []
    for r, row in enumerate(rows):
        row = list(row)
        for r2, c1, c2 in starts.get(r, ()):
            active.append((r2, c1, c2, row[c1] if 0 <= c1 < len(row) else None))
        if active:
            for r2, c1, c2, v in active:
                if c2 >= len(row):
                    row.extend([""] * (c2 + 1 - len(row)))
                for c in range(max(c1, 0), c2 + 1):
                    row[c] = v
            active = [a for a in active if a[0] > r]
        yield row


def _formula_cells(path: Path) -> dict:
    """Positions of formula cells per sheet (xlsx/xlsm only, small files)."""
    if path.suffix.lower() not in (".xlsx", ".xlsm") or path.stat().st_size > FORMULA_SCAN_MAX_BYTES:
        return {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    except Exception:
        return {}
    out = {}
    for ws in wb.worksheets:
        cells = set()
        for row in ws.iter_rows():
            for c in row:
                v = getattr(c, "value", None)
                if isinstance(v, str) and v.startswith("=") and hasattr(c, "row"):
                    cells.add((c.row - 1, c.column - 1))
        out[ws.title] = cells
    wb.close()
    return out


def ingest_excel(path: Path, work_dir: Path, name: str, progress=lambda p, m: None):
    import python_calamine as pc

    try:
        wb = pc.CalamineWorkbook.from_path(str(path))
    except Exception as e:
        msg = str(e)
        if "password" in msg.lower() or "encrypt" in msg.lower():
            raise ValueError("Workbook is password-protected / encrypted; remove the password and retry.")
        raise ValueError(f"Could not open spreadsheet ({type(e).__name__}: {msg[:200]}).")
    formulas = _formula_cells(path)
    tables = []
    metas = wb.sheets_metadata
    for si, meta in enumerate(metas):
        progress(0.05 + 0.4 * si / max(1, len(metas)), f"Reading sheet '{meta.name}'")
        if "WorkSheet" not in str(meta.typ):
            continue   # chart sheets, macro sheets
        sh = wb.get_sheet_by_index(si)
        if not sh.height or not sh.width:
            continue
        log = RepairLog()
        if "Visible" != str(meta.visible).split(".")[-1]:
            log.note("Sheet '{s}' is hidden in the workbook.", s=meta.name)
        merges = list(getattr(sh, "merged_cell_ranges", None) or [])
        merged_cols = set()
        for (r1, c1), (r2, c2) in merges:
            log.add("merged_cells_filled", r1 + 1, f"rows {r1 + 1}-{r2 + 1}, cols {c1 + 1}-{c2 + 1}")
            if r1 < 30:
                merged_cols.update(range(c1, c2 + 1))
        source = {"format": "spreadsheet", "sheet": meta.name}
        if sh.height * sh.width <= MATERIALIZE_MAX_CELLS:
            grid = list(_filled_rows(sh.to_python(skip_empty_area=False), merges, 0, 0))
            fcells = formulas.get(meta.name) or ()
            uncached = [(r, c) for r, c in fcells
                        if r < len(grid) and (c >= len(grid[r]) or grid[r][c] in ("", None))]
            if uncached:
                from .grid import _cell_ref
                cols = sorted({_cell_ref(0, c)[:-1] for _, c in uncached})
                log.add("uncached_formulas", uncached[0][0] + 1,
                        detail=f"{len(uncached)} formula cells in column(s) {', '.join(cols[:10])} have no saved value "
                               "(file was produced by a tool that does not calculate formulas; open & save it in Excel)")
                log.note("Columns {cols} contain formulas without saved results: their values are "
                         "missing in this report.", cols=", ".join(cols[:10]))
                log.counts["uncached_formulas"] = len(uncached)
            tables += grid_to_tables(grid, work_dir, f"{name}_{si}", meta.name, log, source,
                                     merged_header_cols=merged_cols)
        else:
            log.note("Very large sheet streamed row by row: multi-table detection disabled.")
            r_off, c_off = sh.start or (0, 0)   # iter_rows starts at the first used cell
            rows = _filled_rows(sh.iter_rows(), merges, r_off, c_off)
            t = grid_to_table(rows, work_dir / f"{name}_{si}.parquet", meta.name, log, source,
                              merged_header_cols=merged_cols)
            if t is not None and t.n_rows:
                tables.append(t)
    return tables
