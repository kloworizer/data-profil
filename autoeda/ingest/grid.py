"""Turn a 2-D grid of cell values (Excel sheet, PDF table) into clean tables.

Handles the layout anomalies typical of human-made spreadsheets and reports:
  * several tables in one sheet (stacked with blank rows / side by side)
  * title + notes above the table, footnotes / "Source:" lines below it
  * multi-row (merged) headers up to 3 levels, wrapped header lines (PDF)
  * empty spacer columns / rows anywhere
  * total / subtotal rows inside the data ("Total", "Grand Total", "Jumlah")
  * section label rows ("Region: West") -> turned into a `section` column
  * wrapped rows where one record spans several lines (PDF)
  * shifted rows (via RowRepairer)
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from contextvars import ContextVar
from pathlib import Path
from typing import Iterable

from .. import cancel
from ..models import RepairLog, Table, clean_header
from .repair import PERIOD_RE, RowRepairer, learn_classes, looks_like_data, vclass
from .structure import header_depth_guess

HEAD_SCAN = 30
HEADER_ROWS: ContextVar[int | None] = ContextVar("autoeda_header_rows", default=None)   # set from memory
# 'Jumlah' alone is a total; 'Jumlah Penduduk' ('number of residents') is an ordinary indicator
TOTAL_RE = re.compile(r"^\s*(grand\s*-?\s*total|sub\s*-?\s*total|total(es|s)?|sum|overall|totaal|gesamt)\b"
                      r"|^\s*jumlah(\s+(total|keseluruhan|seluruhnya|semua|akhir))?\s*[:.]?\s*$", re.I)
NOTE_RE = re.compile(r"^\s*(source|sumber|note|notes|nb|catatan|keterangan|remark|footnote|disclaimer|\*|†|‡|\(\d+\)|\d+\)|"
                     r"generated|printed|page\s+\d)", re.I)
SECTION_RE = re.compile(r"(:\s*$|^(region|wilayah|area|kategori|category|group|kelompok|section|bagian|"
                        r"divisi|division|dept|department|segment|cabang|branch|year|tahun|month|bulan)\b)", re.I)
INVISIBLE_RE = re.compile("[​‌‍﻿­]")


class FilledStr(str):
    """A cell value copied into a merged range (not present in the source cell itself)."""
    __slots__ = ()


def cell_str(v) -> str:
    if isinstance(v, FilledStr):
        return v
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat(sep=" ")
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    return str(v).replace("\r\n", "\n")


def norm_text(s: str) -> str:
    """NFKC (ligatures, full-width digits) + drop zero-width chars; used for PDF text."""
    return INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", s))




def _texty(row: list[str]) -> float:
    """Share of filled cells that read as labels. Period names used as column
    headers (2019, 2020-Q1, FY23) count as labels too."""
    filled = [c.strip() for c in row if c.strip()]
    if not filled:
        return 0.0
    labels = sum(1 for c in filled if vclass(c) == "T")
    periods = sum(1 for c in filled if vclass(c) != "T" and PERIOD_RE.match(c))
    if periods >= 2 and len(set(filled)) == len(filled):
        labels += periods
    return labels / len(filled)


def _nn(row) -> int:
    return sum(1 for c in row if c.strip())


def _is_title(r) -> bool:
    vals = {c.strip() for c in r if c.strip()}
    return len(vals) == 1


def _header_like(r) -> bool:
    return _nn(r) >= 2 and _texty(r) >= 0.8 and not _is_title(r)


# --- multi-table detection --------------------------------------------------------
def find_blocks(grid: list[list[str]], spans: dict | None = None) -> list[tuple[int, int, int, int]]:
    """Split a materialised sheet into table blocks (r0, r1, c0, c1), inclusive.
    spans: row -> (c0, c1) of a merged label cell; it ties together the columns it covers."""
    rows_nonblank = [i for i, r in enumerate(grid) if any(c.strip() for c in r)]
    if not rows_nonblank:
        return []
    vsegs, start, prev = [], rows_nonblank[0], rows_nonblank[0]
    for i in rows_nonblank[1:]:
        gap = i - prev - 1
        if gap >= 1 and _starts_new_table(grid, start, prev, i, gap):
            vsegs.append((start, prev))
            start = i
        prev = i
    vsegs.append((start, prev))

    blocks = []
    for r0, r1 in vsegs:
        width = max(len(grid[r]) for r in range(r0, r1 + 1))
        bridged = {c for r, (a, b) in (spans or {}).items() if r0 <= r <= r1 for c in range(a, b + 1)}
        used = [c for c in range(width) if c in bridged
                or any(c < len(grid[r]) and grid[r][c].strip() for r in range(r0, r1 + 1))]
        groups, cur = [], [used[0]]
        for c in used[1:]:
            if c - cur[-1] > 1:
                groups.append(cur)
                cur = [c]
            else:
                cur.append(c)
        groups.append(cur)
        # side-by-side tables only when every group is >=2 columns and starts with a header-like row
        if len(groups) > 1 and all(len(g) >= 2 for g in groups) and all(
                _group_has_header(grid, r0, r1, g[0], g[-1]) for g in groups):
            blocks += [(r0, r1, g[0], g[-1]) for g in groups]
        else:
            blocks.append((r0, r1, used[0], used[-1]))
    return blocks


def _group_has_header(grid, r0, r1, c0, c1) -> bool:
    for r in range(r0, min(r1 + 1, r0 + 6)):
        sub = [grid[r][c] if c < len(grid[r]) else "" for c in range(c0, c1 + 1)]
        if _nn(sub):
            if _is_title(sub) and _nn(sub) == 1:
                continue
            return _header_like(sub)
    return False


def _starts_new_table(grid, start, prev, i, gap) -> bool:
    cand = grid[i]
    look = i
    if _nn(cand) == 1 and _texty(cand) == 1.0:     # a title line, header expected right after
        nxt = next((j for j in range(i + 1, min(i + 4, len(grid))) if _nn(grid[j])), None)
        if nxt is None:
            return False
        cand, look = grid[nxt], nxt
    if not _header_like(cand):
        return False
    if gap >= 2:
        return True
    body = [r for r in grid[max(start, prev - 50):prev + 1] if _nn(r)]
    width = max(len(cand), max(len(r) for r in body))
    classes = learn_classes([(r + [""] * width)[:width] for r in body], width)
    return any(classes[c] in "IND" and cand[c].strip() and vclass(cand[c]) == "T" for c in range(len(cand)))


# --- one table ----------------------------------------------------------------------
def _combine_level(header_cells: list[str], nxt: list[str], wrap_only: bool) -> list[str]:
    """Merge one more header row into the names: 'parent / sub', or plain
    concatenation when the header text merely wrapped onto a second line."""
    combined, parent = [], ""
    for i in range(max(len(header_cells), len(nxt))):
        top = header_cells[i] if i < len(header_cells) else ""
        sub = nxt[i] if i < len(nxt) else ""
        if wrap_only:
            combined.append(f"{top} {sub}".strip())         # header text wrapped onto a second line
            continue
        if not top.strip() and not sub.strip():
            combined.append("")                             # spacer column: no inherited name
            continue
        parent = top or parent
        same = parent.strip().lower() == sub.strip().lower()
        combined.append(f"{parent} / {sub}" if parent and sub and not same else (sub or parent))
    return combined


def grid_to_table(rows: Iterable[list], out: Path, name: str, log: RepairLog, source: dict,
                  merged_header_cols: set | None = None, wrapped_rows: bool = False,
                  fixed_header: list[str] | None = None, label_rows: set | None = None,
                  advisor=None) -> Table | None:
    """fixed_header: header already worked out by the caller (rows are then all
    body rows). label_rows: 0-based indexes of rows known to be label rows (a
    merged cell spanning the row), turned into a section column. advisor: a
    StructureAI asked when the rules are unsure about the header depth."""
    materialized = isinstance(rows, list)
    label_rows = label_rows or set()
    it = iter(rows)
    head: list[list[str]] = []
    for r in it:
        head.append([cell_str(v) for v in r])
        if len(head) >= HEAD_SCAN:
            break
    if not any(_nn(r) for r in head):
        return None

    width = max(len(r) for r in head)
    nn = [_nn(r) for r in head]
    maxnn = max(nn)
    hdr = None
    if fixed_header is None:
        hdr = next((i for i, r in enumerate(head) if nn[i] >= max(2, 0.5 * maxnn) and _texty(r) >= 0.6
                    and not _is_title(r)
                    and not looks_like_data(r, [x for j, x in enumerate(head[i + 1:]) if _nn(x) >= 2
                                                and not (j < 2 and _texty(x) >= 0.8)])),   # skip sub-header rows
                   None)
    has_header = hdr is not None or fixed_header is not None
    if fixed_header is not None:
        hdr = -1
    elif hdr is None:
        hdr = next(i for i, k in enumerate(nn) if k > 0)
    for i in range(max(0, hdr)):
        if nn[i]:
            log.add("preamble_skipped", i + 1, " ⎮ ".join(c for c in head[i] if c.strip()))

    header_cells = list(fixed_header) if fixed_header is not None else head[hdr] if has_header else []
    width = max(width, len(header_cells))
    body_start = hdr + 1 if has_header else hdr
    # multi-row header (up to 3 levels): following rows are text while the rows after them are data
    levels = 1
    want = HEADER_ROWS.get()            # header depth learned from user feedback
    if want and has_header and fixed_header is None:
        while levels < want and body_start < len(head):
            header_cells = _combine_level(header_cells, head[body_start], False)
            body_start += 1
            levels += 1
        log.add("multirow_header_combined", hdr + 1, None, " ⎮ ".join(header_cells),
                detail=f"{want} header rows (learned from feedback)")
    while not want and has_header and fixed_header is None and levels < 3 and body_start + 1 < len(head):
        nxt = head[body_start]
        # first real data row after the candidate (skip blanks and one-cell section labels)
        after = next((r for r in head[body_start + 1:] if _nn(r) >= 2), None)
        if after is None:
            break
        repeated = any(header_cells[i] and i + 1 < len(header_cells) and header_cells[i] == header_cells[i + 1]
                       for i in range(len(header_cells) - 1))
        gaps = any(not c.strip() for c in header_cells[:width])
        pdf_wrap = wrapped_rows and _nn(nxt) >= 1 and not any(vclass(c) in "IND" for c in nxt if c.strip())
        after_is_data = _texty(after) < _texty(nxt) and any(vclass(c) in "IND" for c in after if c.strip())
        if not (_texty(nxt) >= 0.8 and after_is_data and (_nn(nxt) >= 2 or pdf_wrap)
                and (repeated or gaps or merged_header_cols or pdf_wrap)):
            break
        combined = _combine_level(header_cells, nxt, pdf_wrap and not (repeated or gaps))
        log.add("multirow_header_combined", hdr + 1, " ⎮ ".join(header_cells), " ⎮ ".join(combined))
        header_cells = combined
        body_start += 1
        levels += 1

    # the rules could not tell whether the next row is one more header level: ask the local model
    if not want and has_header and fixed_header is None and advisor is not None and advisor.enabled and levels < 3:
        raw = [r for r in head[hdr:] if _nn(r)][:12]
        depth, unsure = header_depth_guess(raw, start=levels, limit=3)
        if unsure:
            n = advisor.header_rows(raw, levels)
            if n and n > levels:
                before = " ⎮ ".join(header_cells)
                for _ in range(n - levels):
                    while body_start < len(head) and not _nn(head[body_start]):
                        body_start += 1
                    header_cells = _combine_level(header_cells, head[body_start], False)
                    body_start += 1
                log.add("ai_layout", hdr + 1, before, " ⎮ ".join(header_cells),
                        detail=f"AI: the header spans {n} rows")
                levels = n

    # keep columns that have a header or any value (materialised: look at every row)
    sample_rows = [r for r in head[body_start:] if _nn(r)]
    scan = [[cell_str(v) for v in r] for r in rows[len(head):]] if materialized else []
    width = max([width] + [len(r) for r in scan]) if scan else width
    keep = [i for i in range(width) if (i < len(header_cells) and header_cells[i].strip())
            or any(i < len(r) and r[i].strip() for r in sample_rows)
            or any(i < len(r) and r[i].strip() for r in scan)]
    if fixed_header is not None:
        keep = list(range(width))     # the caller already dropped empty columns
    if not materialized:   # streaming: only trim the right edge, later rows may fill inner columns
        keep = list(range((max(keep) + 1) if keep else width))
    dropped = width - len(keep)
    if dropped and materialized:
        log.add("empty_column_dropped", None, detail=f"{dropped} empty spacer column(s)")
    if not keep:
        return None
    pick = lambda r: [(r[i] if i < len(r) else "") for i in keep]  # noqa: E731
    header = clean_header(pick(header_cells), len(keep)) if has_header else clean_header([], len(keep))
    if not has_header:
        log.note("No header row detected: generic column names generated.")
    w = len(keep)

    # Columns holding values copied down from a merged range. The original column keeps the
    # source as it was (blank under the merge: the real "missing"); the normalised copy goes to
    # an extra "<name> (filled)" column. Needs the whole sheet, so not for streamed sheets.
    comp_idx: list[int] = []
    ff_idx: list[int] = []
    if materialized:
        hit = set()
        for r in sample_rows + scan:
            for j, i in enumerate(keep):
                if j not in hit and i < len(r) and isinstance(r[i], FilledStr) and r[i].strip():
                    hit.add(j)
        # same idea for group labels typed only on the first row of each group (no real merge)
        ff_idx = _ffill_columns([pick(r) for r in sample_rows + scan], w, hit)
        comp_idx = sorted(hit | set(ff_idx))
    ff_set = set(ff_idx)
    names = clean_header(header + [f"{header[j]} (filled)" for j in comp_idx])
    comp_names = names[w:]
    sec_name = clean_header(names + ["section"])[-1]
    for j, cn in zip(comp_idx, comp_names):
        if j in ff_set:
            log.add("ffill_column", None, detail=f"'{header[j]}' -> '{cn}'")
            log.note("Column '{c}' looks like group labels written once per group: it keeps the original "
                     "(blank below the label); the labels carried down are in the extra column '{n}'.",
                     c=header[j], n=cn)
        else:
            log.note("Column '{c}' had merged cells: it keeps the original (blank under the merge); the values "
                     "filled down from the merge are in the extra column '{n}'.", c=header[j], n=cn)
    last: dict[int, str] = {}

    padded = [pick(r) for r in sample_rows]
    classes = learn_classes(padded, w)
    rep = RowRepairer(header, classes, "\t", log)
    from .sink import ParquetSink   # lazy: keeps the header helpers importable without pyarrow
    sink = ParquetSink(out, names + [sec_name])
    state = {"section": None, "used_section": False, "pending": None, "data_rows": 0, "pending_notes": []}

    def flush_pending():
        if state["pending"] is not None:
            row, ln, copied = state["pending"]
            state["pending"] = None
            fixed_rows = rep.fix(row, ln)
            for fixed in fixed_rows:
                out_row = [str(v) for v in fixed]
                comp = []
                for j in comp_idx:
                    v = out_row[j]
                    if j in ff_set:                       # carry the last label down
                        if v.strip():
                            last[j] = v
                        else:
                            v = last.get(j, "")
                    comp.append(v)
                if comp_idx and copied and len(fixed_rows) == 1 and fixed == row:   # row not shifted by a repair
                    out_row = ["" if j in copied else v for j, v in enumerate(out_row)]
                sink.write(out_row + comp + [state["section"]])
                state["data_rows"] += 1
            if state["section"] is not None:
                state["used_section"] = True

    def emit(raw: list[str], line_no: int):
        extra = [c for c in raw[max(keep) + 1:] if c.strip()] if keep else []
        row = pick(raw)
        copied = {j for j, c in enumerate(row) if isinstance(c, FilledStr) and c.strip()} if comp_idx else None
        filled = [(i, c) for i, c in enumerate(row) if c.strip()]
        if not filled and not extra:
            log.counts["empty_row"] += 1
            return
        if line_no % 5000 == 0:
            cancel.check()
        if extra:
            row[-1] = (row[-1] + " " + " ".join(extra)).strip()
            log.add("overflow_joined", line_no)
        if rep.is_header_repeat(row):
            log.add("repeated_header", line_no)
            return
        first = filled[0][1]
        # total / subtotal rows
        if TOTAL_RE.match(first) and any(vclass(c) in "IN" for _, c in filled[1:]):
            log.add("total_row_removed", line_no, " ⎮ ".join(c for _, c in filled))
            return
        # one-cell rows: section label, footnote or wrapped continuation
        if len(filled) == 1 and w >= 2 and vclass(first) == "T":
            idx = filled[0][0]
            if NOTE_RE.match(first) or len(first) > 80:
                log.add("note_row_skipped", line_no, first)
                return
            # a merged cell spanning the row is a label for the rows below it; so is a lone
            # first-column text where the other columns always hold numbers / dates
            # (a capital first letter, so a wrapped line of a long name stays a continuation)
            typed_rest = sum(1 for j, k in enumerate(classes) if j != idx and k in "IND") >= 2
            if (line_no - 1) in label_rows or (
                    w >= 3 and idx <= 1 and (SECTION_RE.search(first) or classes[idx] in "IND"
                                             or (typed_rest and first[:1].isupper()))):
                flush_pending()
                state["section"] = first.rstrip(":").strip()
                log.add("section_row_to_column", line_no, first, detail=f"values copied into column '{sec_name}'")
                return
        # PDF: a record wrapped over several lines -> append text to the previous row
        if (wrapped_rows and state["pending"] is not None and not row[0].strip() and len(filled) <= max(1, w // 2)
                and all(vclass(c) == "T" for _, c in filled)):
            prev = state["pending"][0]
            for i, c in filled:
                prev[i] = (prev[i] + " " + c).strip()
            log.add("wrapped_row_merged", line_no, " ⎮ ".join(c for _, c in filled))
            return
        flush_pending()
        state["pending"] = (row, line_no, copied)

    for i, r in enumerate(head[body_start:], start=body_start + 1):
        emit(r, i)
    if materialized:
        for i, r in enumerate(scan, start=len(head) + 1):
            emit(r, i)
    else:
        for i, r in enumerate(it, start=len(head) + 1):
            emit([cell_str(v) for v in r], i)
    flush_pending()
    n = sink.close()
    t = Table(name=name, path=out, columns=names + [sec_name], n_rows=n, source=source, repairs=log,
              derived={cn: header[j] for j, cn in zip(comp_idx, comp_names)})
    if not state["used_section"]:
        t.drop_columns = [sec_name]
        t.columns = names
    return t


def _ffill_columns(rows: list[list[str]], w: int, skip: set) -> list[int]:
    """Left-hand text columns that look like group labels typed once per group: filled on the first
    row, blank on the rows below, while the other columns of those rows hold values."""
    body = [r for r in rows if _nn(r) >= 2]
    if len(body) < 6 or w < 3:
        return []
    out = []
    for j in range(min(w, max(2, w // 2 + 1))):
        if j in skip:
            continue
        vals = [r[j].strip() for r in body]
        nb = [v for v in vals if v]
        k, m = len(nb), len(vals) - len(nb)
        if not vals[0] or k < 2 or m < k or k > 0.5 * len(vals) or any(len(v) > 60 for v in nb):
            continue
        if sum(vclass(v) == "T" for v in nb) < 0.8 * k:
            continue
        out.append(j)
    return out


def grid_to_tables(grid: list[list], out_dir: Path, base: str, label: str, log: RepairLog, source: dict,
                   merged_header_cols: set | None = None, wrapped_rows: bool = False,
                   label_rows: set | None = None, advisor=None) -> list[Table]:
    """Materialised grid: detect every table block, then normalise each one.
    label_rows: grid row indexes holding one merged label cell (section rows)."""
    sgrid = [[cell_str(v) for v in r] for r in grid]
    blocks = find_blocks(sgrid, label_rows if isinstance(label_rows, dict) else None)
    tables = []
    for bi, (r0, r1, c0, c1) in enumerate(blocks):
        sub = [r[c0:c1 + 1] for r in sgrid[r0:r1 + 1]]
        blog = log if bi == 0 else RepairLog()
        name = label if len(blocks) == 1 else f"{label} [{_cell_ref(r0, c0)}:{_cell_ref(r1, c1)}]"
        if len(blocks) > 1:
            blog.note("Sheet/page holds {n} separate tables; this is table {i} ({rng}).", n=len(blocks), i=bi + 1,
                      rng=f"{_cell_ref(r0, c0)}:{_cell_ref(r1, c1)}")
        t = grid_to_table(sub, out_dir / f"{base}_b{bi}.parquet", name, blog,
                          {**source, "range": f"{_cell_ref(r0, c0)}:{_cell_ref(r1, c1)}"},
                          merged_header_cols=merged_header_cols, wrapped_rows=wrapped_rows,
                          label_rows={r - r0 for r in (label_rows or ()) if r0 <= r <= r1}, advisor=advisor)
        if t is not None and t.n_rows:
            tables.append(t)
    return tables


def _cell_ref(r: int, c: int) -> str:
    s, c = "", c + 1
    while c:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return f"{s}{r + 1}"
