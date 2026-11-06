"""PDF ingestion.

1. Ruled tables (lattice) per page; borderless tables via text alignment when a
   page has no ruled table but looks tabular.
2. Merged cells are resolved from the cell geometry: pdfplumber reports a spanned
   slot as None, and the covering cell tells whether the span is vertical (a group
   value -> copied down), horizontal in the header (a group label -> copied right
   so it prefixes each sub-column) or a whole-row label (section / note -> kept
   once, turned into a section column later).
3. Tables continuing on the next page are stitched: columns are aligned by their
   x-position (tolerates a different number of detected columns), the header
   re-printed at the top of each page is recognised by fuzzy matching and
   removed. The rows repeated on every page also tell how deep the header is,
   so a second header level (years, quarters) is not read as data.
4. When the rules are unsure (a first row that only half matches the header,
   a page that may start a new table, a possible extra header level) the local
   Ollama model is asked; its answers are checked and logged as AI decisions.
5. Wrapped records (one row printed over several lines) are re-joined.
6. No tables at all -> text layer, minus repeated page headers/footers and page
   numbers, parsed as delimited / fixed-width text. Scanned PDFs -> OCR when
   pytesseract + Tesseract are installed.
"""
from __future__ import annotations

import math
import os
import re
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .. import cancel, config
from ..models import RepairLog
from .delimited import ingest_delimited
from .grid import HEADER_ROWS, cell_str, grid_to_table, norm_text
from .repair import learn_classes, vclass
from .structure import StructureAI, combine_header, header_depth_guess, label_only, nn, row_sim, texty

PAGE_NO_RE = re.compile(r"^\s*[-–]?\s*(page|hal(aman)?\.?|p\.|pg\.?|seite|p[áa]gina|p[áa]g\.?)?\s*\d+\s*"
                        r"((of|dari|de|von|/)\s*\d+)?\s*[-–]?\s*$", re.I)
LETTERSPACED_RE = re.compile(r"(?:[\d/.,:-]{1,2} ){3,}[\d/.,:-]{1,2}")
NUMBER_CELL_RE =re.compile(r"^[-+–(]?\s?[$€£¥₹]?\s?\d[\d.,:/%\s'-]*[%)]?$")
CONTINUED_RE =re.compile(r"\b(lanjutan|sambungan|continued|cont\.|fortsetzung)\b", re.I)
HEADER_MATCH = 0.85     # row similarity above which a row is the repeated header
HEADER_MAYBE = 0.5       # between the two: ask the AI
MAX_HEADER_ROWS = 4
LOOK = MAX_HEADER_ROWS + 2
EPS = 0.5


# --- one page table ---------------------------------------------------------------------
def _clean(v) -> str:
    s = norm_text(cell_str(v)).replace("\n", " ").strip()
    # letter-spaced digits ('0 3 / 2 5 / 2 0 16'): close the gaps when only digits and
    # separators remain
    if len(s) >= 7 and LETTERSPACED_RE.fullmatch(s):
        toks = s.split()
        if sum(len(t) == 1 for t in toks) >= 0.75 * len(toks) and any(t in "/.:-" for t in toks):
            s = s.replace(" ", "")
    return s


class PageTable:
    """A table found on one page: cell text plus where every spanned slot comes from."""

    def __init__(self, page: int, grid: list[list], origin: dict, cols: list[tuple[float, float]], ruled: bool,
                 reader: str = "lines"):
        self.page = page
        self.grid = grid              # text, or None for a slot covered by a merged cell
        self.origin = origin          # (r, c) -> (r0, c0) of the covering cell
        self.cols = cols              # x-range per column
        self.ruled = ruled            # cell boundaries known (ruling lines or the table model)
        self.reader = reader          # lines | text (alignment) | docling (TableFormer)
        self.band_hint = 0            # header depth implied by merged header cells (0 = no evidence)
        self.model_header = 0         # header rows flagged by the table model
        self.labels: set[int] = set()  # rows holding one cell merged across the row
        self.names: list[str] | None = None    # column names read by the vision model
        self.vlines: list[float] | None = None  # column boundaries found with the vision model
        self.top: float | None = None          # y of the table's top edge (None = unknown)
        self.bottom: float | None = None

    @property
    def width(self) -> int:
        return len(self.cols)

    def text(self, r, c) -> str:
        v = self.grid[r][c]
        if v is None:
            o = self.origin.get((r, c))
            v = self.grid[o[0]][o[1]] if o else None
        return _clean(v)

    def row(self, r, band: int) -> list[str]:
        """Row r with merged slots resolved, for a table whose header has `band` rows."""
        out = []
        for c in range(self.width):
            v = self.grid[r][c]
            if v is not None:
                out.append(_clean(v))
                continue
            o = self.origin.get((r, c))
            if o is None:
                out.append("")
            elif r < band:
                out.append(self.text(*o))                 # header: group label over each of its columns
            elif o[0] == r:
                out.append("")          # horizontal span in the body: one value, kept in its first column
            elif o[0] < band:
                out.append("")                            # header cell reaching into the body
            else:
                out.append(self.text(*o))                 # vertical span: group value copied down
        return out

    def spans(self, r: int, band: int) -> int:
        """Slots of row r filled from a merged cell above it."""
        n = 0
        for c in range(self.width):
            o = self.origin.get((r, c)) if self.grid[r][c] is None else None
            if o and band <= o[0] < r:
                n += 1
        return n


def _page_tables(page, pi: int, settings=None) -> list[PageTable]:
    out = []
    for tb in (page.find_tables(settings) if settings else page.find_tables()):
        grid = tb.extract()
        cells = tb.cells
        if not grid or len(grid) < 2 or not cells:
            continue
        w = max(len(r) for r in grid)
        grid = [list(r) + [None] * (w - len(r)) for r in grid]
        xs = sorted({c[0] for c in cells})
        tops = sorted({c[1] for c in cells})
        origin = {}
        if len(xs) == w and len(tops) == len(grid):
            xi = {x: i for i, x in enumerate(xs)}
            ti = {t: i for i, t in enumerate(tops)}
            for cell in cells:
                r0, c0 = ti[cell[1]], xi[cell[0]]
                r1 = max(i for i, t in enumerate(tops) if t < cell[3] - EPS)
                c1 = max(i for i, x in enumerate(xs) if x < cell[2] - EPS)
                for r in range(r0, r1 + 1):
                    for c in range(c0, c1 + 1):
                        if (r, c) != (r0, c0):
                            origin[(r, c)] = (r0, c0)
            right = max(c[2] for c in cells)
            cols = [(xs[i], xs[i + 1] if i + 1 < w else right) for i in range(w)]
        else:   # geometry does not line up with the text grid: no span information
            x0, x1 = tb.bbox[0], tb.bbox[2]
            cols = [(x0 + (x1 - x0) * i / w, x0 + (x1 - x0) * (i + 1) / w) for i in range(w)]
        grid, origin = _split_stacked(grid, origin)
        pt = PageTable(pi, grid, origin, cols, settings is None, "lines" if settings is None else "text")
        pt.top, pt.bottom = tb.bbox[1], tb.bbox[3]
        _mark_labels(pt)
        out.append(pt)
    return out


def _not_watermark(obj) -> bool:
    """False for characters set at a slant (watermarks such as a site URL across the
    page); upright and vertical (90°) text is kept."""
    if obj.get("object_type") != "char" or "matrix" not in obj:
        return True
    a, b = obj["matrix"][0], obj["matrix"][1]
    angle = abs(math.degrees(math.atan2(b, a))) % 90
    return angle < 5 or angle > 85


def _split_stacked(grid: list[list], origin: dict) -> tuple[list[list], dict]:
    """A table ruled between columns but not between rows puts several records into one
    cell, one per text line. A row whose filled cells all hold the same number of lines
    (and some of them numbers) is split into that many rows. Rows touched by a merged
    cell are left alone."""
    spanned = {r for (r, _), (r0, _) in origin.items()} | {r0 for (r0, _) in origin.values()}
    out, moved, shift = [], {}, 0
    for r, row in enumerate(grid):
        cells = [str(v).split("\n") for v in row if v]
        k = len(cells[0]) if cells else 0
        if r == 0 or r in spanned or None in row or k < 2 or len(cells) < 2 \
                or any(len(c) != k for c in cells) \
                or not any(all(NUMBER_CELL_RE.match(x.strip()) for x in c) for c in cells):
            moved[r] = r + shift
            out.append(row)
            continue
        for i in range(k):
            out.append([str(v).split("\n")[i] if v else v for v in row])
        moved[r] = r + shift
        shift += k - 1
    if not shift:
        return grid, origin
    return out, {(moved[r], c): (moved[r0], c0) for (r, c), (r0, c0) in origin.items()}


def _segments(line: list[dict], gap: float = 0.75) -> list[list[dict]]:
    """Split a text line into phrases at gaps wider than `gap` line heights."""
    h = sorted(w["bottom"] - w["top"] for w in line)[len(line) // 2]
    segs = [[line[0]]]
    for w in line[1:]:
        if w["x0"] - segs[-1][-1]["x1"] > max(2.0, gap * h):
            segs.append([w])
        else:
            segs[-1].append(w)
    return segs


def _row_like(segs: list[list[dict]]) -> bool:
    """A line that reads as a table row: several short phrases, or a label and numbers
    (prose gives one or two long phrases per line)."""
    if len(segs) < 2:
        return False
    texts = [" ".join(w["text"] for w in s) for s in segs]
    numeric = sum(1 for t in texts if re.search(r"\d", t) and len(t) <= 20)
    return (len(segs) >= 3 or numeric >= 1) and sum(len(t) for t in texts) / len(texts) <= 30


def _gutters(lines: list[list[dict]], x0: float, x1: float) -> list[float]:
    """Column boundaries: x-ranges that no phrase of the block's rows covers (a phrase,
    not a word: 'Item 0' on every row must not open a gutter after 'Item')."""
    step = 1.0
    n = int((x1 - x0) / step) + 1
    cover = [0] * n
    for line in lines:
        for seg in _segments(line, 0.45):          # words one plain space apart stay together
            for i in range(int((seg[0]["x0"] - x0) / step), min(n, int((seg[-1]["x1"] - x0) / step) + 1)):
                cover[i] += 1
    limit = max(1, int(0.03 * len(lines)))      # a value glued to its neighbour may cross a gutter
    out, i = [], 0
    while i < n:
        if cover[i] <= limit:
            j = i
            while j < n and cover[j] <= limit:
                j += 1
            if j - i >= 2 and i > 0 and j < n:          # inner gutters only, at least 2pt wide
                out.append(x0 + (i + j) / 2 * step)
            i = j
        else:
            i += 1
    # a gutter crossed by a few lines only stands when the columns on both sides hold values
    # on many lines (not just the overflow of one long cell)
    while True:
        edges = [x0 - 1, *out, x1 + 1]
        used = [sum(1 for l in lines if any(edges[k] <= (w["x0"] + w["x1"]) / 2 < edges[k + 1] for w in l))
                for k in range(len(edges) - 1)]
        weak = [k for k in range(len(out)) if min(used[k], used[k + 1]) < 0.2 * len(lines)
                and any(cover[int((out[k] - x0) / step) + d] for d in (-1, 0, 1)
                        if 0 <= int((out[k] - x0) / step) + d < n)]
        if not weak:
            return out
        out.pop(weak[0])


def _stream_tables(page, pi: int) -> list[PageTable]:
    """Borderless tables from text alignment: runs of row-like lines, cut into columns
    at the whitespace gutters shared by all their lines, so no word is ever split."""
    words = page.extract_words(x_tolerance=1.5)
    if len(words) < 6:
        return []
    lines = _lines_of(words)
    segs = [_segments(l) for l in lines]
    # page furniture: a line that is only a page number, or names a page ('Página 12 de 14')
    furniture = [any(PAGE_NO_RE.match(t) and (len(ss) == 1 or re.search(r"[^\W\d_]", t))
                     for t in (" ".join(w["text"] for w in s) for s in ss)) for ss in segs]
    flags = [_row_like(ss) and not f for ss, f in zip(segs, furniture)]
    blocks, i = [], 0
    while i < len(lines):
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(lines):           # one non-row line inside (a wrapped cell, a section label) is kept
            if flags[j + 1]:
                j += 1
            elif j + 2 < len(lines) and flags[j + 2] and not furniture[j + 1] \
                    and _close(lines[j], lines[j + 1], 1.6) and _close(lines[j + 1], lines[j + 2], 2.6):
                j += 2
            else:
                break
        if sum(flags[i:j + 1]) >= 3:
            blocks.append((i, j))
        i = j + 1
    out = []
    for i, j in blocks:
        body = [l for l, f in zip(lines[i:j + 1], flags[i:j + 1]) if f]
        # header lines directly above: short phrases, no big vertical gap
        top = i
        while top > 0 and i - top < MAX_HEADER_ROWS and _close(lines[top - 1], lines[top], 1.8) \
                and not furniture[top - 1] and all(len(" ".join(w["text"] for w in s)) <= 40 for s in segs[top - 1]):
            top -= 1
        x0 = min(w["x0"] for l in body for w in l)
        x1 = max(w["x1"] for l in body for w in l)
        cuts = _gutters(body, x0, x1)
        if not cuts:
            continue
        edges = [min(x0, min(w["x0"] for l in lines[top:j + 1] for w in l)) - 1, *cuts,
                 max(x1, max(w["x1"] for l in lines[top:j + 1] for w in l)) + 1]
        grid = []
        for li in range(top, j + 1):
            row = [""] * (len(edges) - 1)
            # body lines word by word (a narrow gutter may hide inside a phrase); header lines
            # phrase by phrase (a heading may reach over a gutter of the body)
            units = [[w] for w in lines[li]] if li >= i else segs[li]
            for u in units:
                mid = (u[0]["x0"] + u[-1]["x1"]) / 2
                c = next((k for k in range(len(edges) - 1) if mid < edges[k + 1]), len(edges) - 2)
                row[c] = f"{row[c]} {' '.join(w['text'] for w in u)}".strip()
            grid.append(row)
        cols = [(edges[k], edges[k + 1]) for k in range(len(edges) - 1)]
        pt = PageTable(pi, grid, {}, cols, False, "text")
        pt.top, pt.bottom = lines[top][0]["top"], lines[j][0]["bottom"]
        out.append(pt)
    return out


def _better_than_rules(found: list[PageTable], page, pi: int, log: RepairLog) -> list[PageTable]:
    """Ruling lines that are not cell borders (row shading, a frame round a borderless
    table) give a table with too few columns. When the text alignment over the same area
    shows clearly more columns and reads at least as well, it replaces that table."""
    alt = [t for t in (_trim(t, log) for t in _stream_tables(page, pi)) if t
           and _looks_tabular([t.row(r, 0) for r in range(len(t.grid))])]
    def same_stretch(pt, st):
        if pt.top is None or st.top is None:
            return False
        common = min(pt.bottom, st.bottom) - max(pt.top, st.top)
        return common >= 0.5 * (pt.bottom - pt.top) and common >= 0.5 * (st.bottom - st.top)

    # where the alignment reading spans several ruled tables, the rules do hold structure
    alt = [st for st in alt if sum(1 for pt in found if same_stretch(pt, st)) == 1]
    out = []
    for pt in found:
        best = None
        for st in alt:
            if not same_stretch(pt, st):
                continue
            if st.width >= pt.width + 2 and _score([st]) >= _score([pt]) - 0.02:
                best = st if best is None or st.width > best.width else best
        if best is not None:
            log.note("Page {p}: the table's lines do not separate its columns; read from text alignment.", p=pi + 1)
        out.append(best or pt)
    return out


def _close(a: list[dict], b: list[dict], factor: float) -> bool:
    """Line b follows line a within `factor` line heights."""
    h = max(w["bottom"] - w["top"] for w in a)
    return b[0]["top"] - a[0]["bottom"] <= factor * h


@contextmanager
def _timed(tm: Counter, name: str):
    t = time.perf_counter()
    try:
        yield
    finally:
        tm[name] += time.perf_counter() - t


@contextmanager
def _shutdown_on_exit(box: list):
    """Stop the worker pool (and drop pages not started) however the page loop ends: done, error, cancel."""
    try:
        yield
    finally:
        for ex in box:
            ex.shutdown(wait=False, cancel_futures=True)


def _prefetch_model(path: Path, pdf, engine: str, n_pages: int, tm: Counter):
    """Start the table model on every page that will need it, in worker processes, so the pages
    are read in parallel while the main loop (cheap geometry, stitching) follows behind.
    Returns (executor, {page: future}); (None, {}) when workers = 1 or nothing qualifies."""
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    from . import pdf_docling

    if config.DOCLING_WORKERS <= 1:
        return None, {}
    with _timed(tm, "prefetch scan"):
        cands = []
        for pi, raw in enumerate(pdf.pages):
            if len(cands) >= config.DOCLING_MAX_PAGES:
                break
            page = raw.dedupe_chars().filter(_not_watermark)
            if engine != "docling" and page.find_tables():
                raw.flush_cache()
                continue     # ruled tables: the geometry reader handles it
            if engine == "docling" or _numeric_page(page) or any(
                    _looks_tabular([t.row(r, 0) for r in range(len(t.grid))]) for t in _stream_tables(page, pi)):
                cands.append(pi)
            raw.flush_cache()
            cancel.check()
    if len(cands) < 2:
        return None, {}
    try:
        key = pdf_docling._file_key(path) if config.DOCLING_CACHE else None
        ex = ProcessPoolExecutor(max_workers=min(config.DOCLING_WORKERS, len(cands)),
                                 mp_context=multiprocessing.get_context("spawn"))
        return ex, {pi: ex.submit(pdf_docling.page_raw, str(path), pi, key) for pi in cands}
    except Exception:
        return None, {}


def _docling_tables(path: Path, pi: int, raw=None) -> list[PageTable]:
    from . import pdf_docling

    def make(grid, origin, cols, header_rows, sections):
        pt = PageTable(pi, grid, origin, cols, True, "docling")
        pt.model_header = header_rows
        _mark_labels(pt)
        pt.labels |= {r for r in sections if r >= header_rows}
        return pt
    return pdf_docling.page_tables(path, pi, make, raw)


def _lines_of(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= 3:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in lines]


def _vision_vlines(words: list[dict], cells: set[str], width: int) -> list[float] | None:
    """Column boundaries from the cells the vision model read: on each text line that
    splits exactly into `width` known cells, the cells' word positions give each
    column's x-range; boundaries go midway between neighbouring columns."""
    by_first: dict[str, list[list[str]]] = {}
    for c in cells:
        toks = c.split()
        by_first.setdefault(toks[0], []).append(toks)
    spans: list[list[tuple[float, float]]] = []
    for line in _lines_of(words):
        segs, i = [], 0
        while i < len(line):
            cands = [t for t in by_first.get(line[i]["text"], []) if [w["text"] for w in line[i:i + len(t)]] == t]
            if not cands:
                break
            t = max(cands, key=len)
            segs.append((line[i]["x0"], line[i + len(t) - 1]["x1"]))
            i += len(t)
        if i == len(line) and len(segs) == width:
            spans.append(segs)
    if len(spans) < 2:
        return None
    xs = [min(s[0][0] for s in spans) - 2]
    for c in range(width - 1):
        right = max(s[c][1] for s in spans)
        left = min(s[c + 1][0] for s in spans)
        if right >= left:
            return None
        xs.append((right + left) / 2)
    xs.append(max(s[-1][1] for s in spans) + 2)
    return xs


def _explicit_tables(page, pi: int, xs: list[float]) -> list[PageTable]:
    settings = {"vertical_strategy": "explicit", "explicit_vertical_lines": xs, "horizontal_strategy": "text",
                "snap_tolerance": 3, "join_tolerance": 3, "min_words_horizontal": 1, "intersection_tolerance": 5}
    out = _page_tables(page, pi, settings)
    for pt in out:
        pt.reader = "vision"
        pt.vlines = xs
    return out


def _vision_table(page, pi: int, ai: StructureAI, log: RepairLog) -> PageTable | None:
    """Let the vision model work out the column layout of a borderless table; the
    values still come from the PDF text layer, cut at the boundaries it implies."""
    import base64
    import io

    words = page.extract_words()
    buf = io.BytesIO()
    page.to_image(resolution=config.VISION_DPI).original.save(buf, "PNG")
    lay = ai.vision_layout(base64.b64encode(buf.getvalue()).decode(), {w["text"] for w in words})
    if not lay:
        return None
    xs = _vision_vlines(words, lay["cells"], lay["width"])
    if not xs:
        return None
    tables = [t for t in (_trim(t, log) for t in _explicit_tables(page, pi, xs)) if t]
    if not tables:
        return None
    pt = max(tables, key=lambda t: len(t.grid))
    # the header ends where the first cell the model read as data appears
    first = next((r for r in range(len(pt.grid)) if any(pt.text(r, c) in lay["cells"] for c in range(pt.width))), 0)
    pt.band_hint = min(first, MAX_HEADER_ROWS)
    if lay["columns"] and len(lay["columns"]) == pt.width:
        pt.names = lay["columns"]
    log.add("ai_layout", None, None, " ⎮ ".join(lay["columns"] or []),
            detail=f"AI (vision): column layout of the table on page {pi + 1}")
    return pt


def _doubtful(tables: list[PageTable]) -> bool:
    """A text-alignment reading worth a second look: no clear header row on top,
    word fragments as cells, or values glued together."""
    if not tables:
        return True
    pt = tables[0]
    top = [pt.text(0, c) for c in range(pt.width)]
    frag = any(len(v) <= 2 and v.isalpha() for v in top if v)
    return not label_only(top) or nn(top) < 0.6 * pt.width or frag or _score(tables) < 0.9


def _numeric_page(page) -> bool:
    """Enough numbers on the page to be worth looking for a table."""
    words = page.extract_words()
    return len(words) >= 20 and sum(1 for w in words if any(ch.isdigit() for ch in w["text"])) >= 0.15 * len(words)


def _choose(geo: list[PageTable], model: list[PageTable], ai: StructureAI, log: RepairLog,
            page: int) -> tuple[list[PageTable], str]:
    """Keep the better of two readings of a page; the local model breaks near-ties."""
    if not model:
        return geo, "geometry"
    if not geo:
        return model, "docling"
    sg, sm = _score(geo), _score(model)
    if abs(sg - sm) < 0.05:
        sample = lambda ts: [ts[0].row(r, 0) for r in range(min(6, len(ts[0].grid)))]  # noqa: E731
        pick = ai.pick_reading(sample(geo), sample(model))
        if pick is not None:
            log.add("ai_layout", None, None, None,
                    detail=f"AI: page {page} reading {'from text alignment' if pick == 0 else 'by the table model'}")
            return (geo, "geometry") if pick == 0 else (model, "docling")
    return (model, "docling") if sm > sg else (geo, "geometry")


def _glued(v: str) -> bool:
    """Two values read into one cell ('41 2024-05-14', 'K287 28')."""
    toks = v.split()
    return len(toks) >= 2 and sum(1 for t in toks if vclass(t) in "IND") >= 1 and len({vclass(t) for t in toks}) >= 2


def _score(tables: list[PageTable]) -> float:
    """Quality of a page reading: columns of one consistent kind, few glued values,
    few holes. Comparable between readers of the same page."""
    total = cells = 0.0
    for pt in tables:
        band = max(pt.band_hint, pt.model_header, 1)
        body = [pt.row(r, band) for r in range(band, len(pt.grid))]
        if not body:
            continue
        w = pt.width
        classes = learn_classes(body, w)
        filled = [(c, v) for r in body for c, v in enumerate(r) if v]
        n = len(body) * w
        if not filled:
            continue
        pure = sum(1 for c, v in filled if classes[c] in "TA" or vclass(v) == classes[c]
                   or {vclass(v), classes[c]} == {"I", "N"}) / len(filled)
        glued = sum(1 for _, v in filled if _glued(v)) / len(filled)
        s = pure - 2 * glued + 0.2 * len(filled) / n
        total += s * n
        cells += n
    return total / cells if cells else -1.0


def _mark_labels(pt: PageTable):
    """Rows whose only content is one cell merged across several columns."""
    pt.labels = set()
    for r in range(len(pt.grid)):
        owners = set()
        for c in range(pt.width):
            o = pt.origin.get((r, c), (r, c)) if pt.grid[r][c] is None else (r, c)
            if pt.text(*o):
                owners.add(o)
        if len(owners) == 1:
            o = next(iter(owners))   # merged across the row, not merely one filled cell
            if o[0] == r and any(pt.origin.get((r, c)) == o for c in range(pt.width)):
                pt.labels.add(r)


def _band_hint(pt: PageTable) -> int:
    """Header depth implied by merged cells in the first row: a label merged down
    over several rows ('Region' beside 'Sales' / '2023'), or a group label merged
    across columns above distinct sub-labels. 0 = no evidence."""
    if not pt.origin or len(pt.grid) < 3:
        return 0
    if not label_only([pt.text(0, c) for c in range(pt.width)]):
        return 0
    depth = 1
    for c in range(pt.width):
        if pt.grid[0][c] is None:
            o = pt.origin.get((0, c))
            if o and o[0] == 0 and pt.text(1, c) and pt.text(1, o[1]) and pt.text(1, c) != pt.text(1, o[1]):
                depth = max(depth, 2)
            continue
        down = sum(1 for (r, cc), o in pt.origin.items() if o == (0, c) and cc == c)
        depth = max(depth, 1 + down)
    depth = min(depth, MAX_HEADER_ROWS, len(pt.grid) - 1)
    # rows inside the band hold labels only (their own cells; merged-down slots are None)
    for r in range(1, depth):
        own = [_clean(pt.grid[r][c]) for c in range(pt.width) if pt.grid[r][c] is not None]
        if any(v and not label_only([v, v]) for v in own):
            depth = r
            break
    return depth if depth > 1 else 0


def _drop_row(pt: PageTable, r: int):
    for (rr, c), o in list(pt.origin.items()):
        if o[0] == r and rr != r:
            pt.grid[rr][c] = ""        # a merge from the dropped (title) row leaves empty slots
    pt.grid.pop(r)
    pt.origin = {(rr - (rr > r), c): (o[0] - (o[0] > r), o[1]) for (rr, c), o in pt.origin.items()
                 if rr != r and o[0] != r}
    pt.labels = {x - (x > r) for x in pt.labels if x != r}
    if r < pt.model_header:
        pt.model_header -= 1


def _trim(pt: PageTable, log: RepairLog) -> PageTable | None:
    """Drop title lines inside the table frame and page numbers at its end, then
    read the header depth from the merged cells."""
    while len(pt.grid) > 2:
        r = [pt.text(0, c) for c in range(pt.width)]
        vals = {v for v in r if v}
        if len(vals) == 1 and pt.width > 1 and (0 in pt.labels or sum(1 for v in r if v) == 1):
            log.add("preamble_skipped", None, next(iter(vals)))
            _drop_row(pt, 0)
            continue
        break
    while len(pt.grid) > 2:
        filled = [v for v in (pt.text(len(pt.grid) - 1, c) for c in range(pt.width)) if v]
        if len(filled) == 1 and PAGE_NO_RE.match(filled[0]):
            _drop_row(pt, len(pt.grid) - 1)
            continue
        break
    if pt.width < 2 or len(pt.grid) < 2:
        return None
    pt.band_hint = max(_band_hint(pt), min(pt.model_header, MAX_HEADER_ROWS, len(pt.grid) - 1)
                       if pt.model_header > 1 else 0)
    return pt


def _looks_tabular(rows: list[list[str]]) -> bool:
    """Reject text-strategy 'tables' that are really prose."""
    if len(rows) < 3 or len(rows[0]) < 2:
        return False
    cells = [c for r in rows for c in r]
    filled = [c for c in cells if c]
    if len(filled) < 0.4 * len(cells):
        return False
    avg_len = sum(len(c) for c in filled) / len(filled)
    numbers = sum(1 for c in filled if NUMBER_CELL_RE.match(c)) / len(filled)
    words = [len(c.split()) for c in filled]
    long = sum(1 for n in words if n >= 5) / len(filled)        # sentence fragments
    lower = sum(1 for c in filled if c[0].islower()) / len(filled)   # a sentence carried on mid-line
    return avg_len < 40 and long <= 0.3 and lower < 0.3         and (numbers >= 0.1 or sum(words) / len(words) <= 2.5)


def _column_numbers(row: list[str]) -> bool:
    """The numbering row some statistical tables print under the header: (1) (2) (3)..."""
    vals = [v for v in row if v]
    nums = [re.fullmatch(r"\(?(\d{1,2})\)?(?:\s*=.*)?", v) for v in vals]     # '2= (3 to 6)+14' too
    if len(vals) < 2 or not all(nums):
        return False
    n = [int(m.group(1)) for m in nums]
    return n == sorted(n) and len(set(n)) == len(n)


def _tabular_text(t) -> bool:
    """A table parsed from the PDF text layer that holds rows and columns, not prose
    cut at spaces."""
    import pyarrow.parquet as pq

    if len(t.columns) < 2:
        return False
    cols = [c for c in t.columns if c not in t.drop_columns]
    data = pq.read_table(t.path, columns=cols).slice(0, 200).to_pylist()
    rows = [list(t.columns)] + [[cell_str(r[c]) for c in cols] for r in data]
    filled = [v for r in rows for v in r if v]
    # running text cut at spaces: cells are single ordinary words
    words = sum(1 for v in filled if re.fullmatch(r"[a-zà-ÿ][\w'’-]*[.,;:]?", v)) / max(1, len(filled))
    numbers = sum(1 for v in filled if NUMBER_CELL_RE.match(v)) / max(1, len(filled))
    return _looks_tabular(rows) and words < 0.2 and numbers >= 0.1


def _ocr_page(page) -> str:
    import pytesseract
    img = page.to_image(resolution=300).original
    return pytesseract.image_to_string(img, lang=config.OCR_LANG)


# --- stitching across pages ------------------------------------------------------------
def _lines_above(page, top: float | None) -> list[str] | None:
    """Text lines printed above a table on its page, top to bottom (page numbers left
    out). None when the table position is unknown."""
    if top is None:
        return None
    words = [w for w in page.extract_words() if w["bottom"] <= top + 1]
    out = []
    for line in _lines_of(words):
        s = norm_text(" ".join(w["text"] for w in line)).strip()
        if s and not PAGE_NO_RE.match(s):
            out.append(s)
    return out


def _caption(above: list[str] | None, prev_above: list[str] | None) -> str | None:
    """A title above the table that the previous page did not print as well (a running
    header would be on both, digits aside): the page starts a new table.
    '(continued)' notes don't count."""
    if not above:
        return None
    seen = {re.sub(r"\d+", "#", l) for l in prev_above or []}
    new = [l for l in above if re.sub(r"\d+", "#", l) not in seen]
    if not new or any(CONTINUED_RE.search(l) for l in new):
        return None
    return " / ".join(new)


def _side_by_side(a: PageTable, b: PageTable) -> bool:
    """b carries on a wide table to the right: same number of rows, the same row labels
    in its first column, but other column headings (statistical yearbooks print a wide
    table over two pages this way)."""
    if len(a.grid) != len(b.grid) or len(a.grid) < 4:
        return False
    keys = [(a.text(r, 0), b.text(r, 0)) for r in range(len(a.grid))]
    filled = [k for k in keys if k[0] or k[1]]
    same = sum(1 for x, y in filled if x == y)
    head_a = [a.text(0, c) for c in range(1, a.width)]
    head_b = [b.text(0, c) for c in range(1, b.width)]
    return len(filled) >= 4 and same >= 0.9 * len(filled) and row_sim(head_a, head_b) < HEADER_MAYBE


def _join_columns(a: PageTable, b: PageTable):
    """Append b's columns (all but its row-label column) to a, in place."""
    w = a.width
    a.grid = [ra + rb[1:] for ra, rb in zip(a.grid, b.grid)]
    for (r, c), (r0, c0) in b.origin.items():
        if c >= 1 and c0 >= 1:
            a.origin[(r, c - 1 + w)] = (r0, c0 - 1 + w)
    shift = a.cols[-1][1] - b.cols[1][0] + 1
    a.cols = a.cols + [(x0 + shift, x1 + shift) for x0, x1 in b.cols[1:]]
    a.band_hint = max(a.band_hint, b.band_hint)
    _mark_labels(a)


def _align(ref: list[tuple[float, float]], cols: list[tuple[float, float]]) -> list[int] | None:
    """Map each column of a page table onto the reference columns by x-overlap.
    None when the tables do not line up (a different table)."""
    if len(ref) == len(cols) and all(abs(a[0] - b[0]) < 25 for a, b in zip(ref, cols)):
        return list(range(len(cols)))
    if abs(cols[0][0] - ref[0][0]) > 30 or abs(cols[-1][1] - ref[-1][1]) > 30:
        return None
    out = []
    for x0, x1 in cols:
        ov = [max(0.0, min(x1, b) - max(x0, a)) for a, b in ref]
        best = max(range(len(ref)), key=lambda i: ov[i])
        if ov[best] < 0.5 * (x1 - x0):
            return None
        out.append(best)
    return out


def _remap(row: list[str], mapping: list[int] | None, width: int) -> list[str]:
    if not mapping:
        return row
    out = [""] * width
    for v, j in zip(row, mapping):
        if v:
            out[j] = f"{out[j]} {v}".strip()
    return out


class Group:
    """One logical table, possibly spread over several pages."""

    def __init__(self, first: PageTable):
        self.first = first
        self.parts: list[tuple[PageTable, list[int] | None, int]] = [(first, None, 0)]  # table, mapping, rows skipped
        self.last_page = first.page
        self.repeat_depth = 0       # header rows found re-printed on the following pages
        self.stats = Counter()

    @property
    def width(self) -> int:
        return self.first.width

    def head_rows(self) -> list[list[str]]:
        return [self.first.row(r, LOOK) for r in range(min(LOOK, len(self.first.grid)))]

    def tail_rows(self, n: int) -> list[list[str]]:
        pt, mapping, skip = self.parts[-1]
        lo = max(skip if pt is not self.first else max(1, pt.band_hint), len(pt.grid) - n)
        return [_remap(pt.row(r, 0), mapping, self.width) for r in range(lo, len(pt.grid))]


def _leading_header_rows(g: Group, rows: list[list[str]], ai: StructureAI, log: RepairLog,
                         page: int) -> int | None:
    """How many top rows of a continuation page re-print the header.
    None = the page starts a different table."""
    head = g.head_rows()
    k = j = 0
    while j < min(MAX_HEADER_ROWS, len(rows) - 1):
        best, at = max(((row_sim(rows[j], h), i) for i, h in enumerate(head) if i >= k), default=(0.0, 0))
        if best >= HEADER_MATCH:
            k, j = at + 1, j + 1
            continue
        if best >= HEADER_MAYBE and label_only(rows[j]):
            verdict = ai.is_repeated_header(head[:max(2, k)], rows[j])
            if verdict is not None:
                log.add("ai_layout", None, " ⎮ ".join(rows[j]), None,
                        detail=f"AI: the first row on page {page} is {'the repeated header' if verdict else 'data'}")
            if verdict:
                k, j = at + 1, j + 1
                continue
        break
    if j == 0 and label_only(rows[0]):
        # labels where the table so far holds numbers or dates: a header of another table?
        tail = g.tail_rows(10)
        classes = learn_classes(tail, g.width)
        if any(classes[i] in "IND" and vclass(v) == "T" for i, v in enumerate(rows[0][:g.width])):
            verdict = ai.continues(head[:2], tail, rows[:5])
            if verdict is not None:
                log.add("ai_layout", None, " ⎮ ".join(rows[0]), None,
                        detail=f"AI: the table on page {page} {'continues' if verdict else 'is a new table'}")
            return 0 if verdict else None
    return j


def _header_band(g: Group, ai: StructureAI, log: RepairLog) -> int:
    """Header depth from merged header cells and from the rows re-printed on every
    page; otherwise from value types, with the local model settling unclear cases.
    0 = no header found (grid_to_table then looks for one itself)."""
    first = g.first
    want = HEADER_ROWS.get()             # header depth learned from user feedback
    if want:
        return min(want, len(first.grid) - 1)
    known = max(first.band_hint, g.repeat_depth)
    if known:
        return min(known, MAX_HEADER_ROWS, len(first.grid) - 1)
    rows = [first.row(r, LOOK) for r in range(min(12, len(first.grid)))]
    if not label_only(rows[0]):
        return 0
    depth, unsure = header_depth_guess(rows, start=1, limit=MAX_HEADER_ROWS)
    if unsure:
        n = ai.header_rows(rows, depth)
        if n is not None and n != depth:
            log.add("ai_layout", 1, None, None, detail=f"AI: the header spans {n} rows (rules: {depth})")
            depth = n
    return depth


def _group_to_table(g: Group, gi: int, ai: StructureAI, log: RepairLog, work_dir: Path, name: str):
    stats = g.stats
    band = _header_band(g, ai, log)
    first = g.first
    head = [first.row(r, band) for r in range(band)]
    head = [h for h in head if not _column_numbers(h)] or head      # '(1) (2) (3)' is no heading
    body: list[list[str]] = []
    labels: set[int] = set()
    for pt, mapping, skip in g.parts:
        start = band if pt is first else skip
        for r in range(start, len(pt.grid)):
            stats["merged_cells_filled"] += pt.spans(r, start)
            if r in pt.labels:
                labels.add(len(body))
            body.append(_remap(pt.row(r, start), mapping, g.width))
    # header rows printed again inside the body (a table split without a page break);
    # blank grid lines (borderless tables leave them between text lines)
    kept, kept_labels = [], set()
    for i, row in enumerate(body):
        if not nn(row):
            continue
        if head and nn(row) >= 2 and any(row_sim(row, h) >= 0.9 for h in head):
            stats["repeated_header"] += 1
            continue
        if not kept and _column_numbers(row):
            stats["repeated_header"] += 1      # '(1) (2) (3)' printed under the header
            continue
        if i in labels:
            kept_labels.add(len(kept))
        kept.append(row)
    body, labels = kept, kept_labels
    # empty spacer columns, decided over the whole stitched table rather than page by page
    keep = [c for c in range(g.width) if any(r[c] for r in body) or any(h[c] for h in head)]
    if len(keep) < g.width:
        stats["empty_column_dropped"] += g.width - len(keep)
    body = [[r[c] for c in keep] for r in body]
    head = [[h[c] for c in keep] for h in head]
    names = [first.names[c] for c in keep] if first.names and len(first.names) == g.width else None
    if len(keep) < 2 or not body:
        return None

    a, b = first.page + 1, g.last_page + 1
    log.note("Table extracted from PDF pages {a}–{b}.", a=a, b=b)
    if len(g.parts) > 1:
        log.note("Table continues over {n} pages; the header repeated on each page was removed.", n=len(g.parts))
    header = names or (combine_header(head, carry=not first.ruled) if head else None)
    if band > 1:
        log.add("multirow_header_combined", 1, " ⎮ ".join(" ⎮ ".join(h) for h in head),
                " ⎮ ".join(header or []), detail=f"{band} header rows")
    for k, v in stats.items():
        if v:
            log.counts[k] += v
    return grid_to_table(body, work_dir / f"{name}_t{gi}.parquet", f"table_{gi + 1} (p.{a})",
                         log, {"format": "pdf", "pages": f"{a}-{b}"},
                         wrapped_rows=True, fixed_header=header, label_rows=labels,
                         advisor=None if header else ai)


def ingest_pdf(path: Path, work_dir: Path, name: str, progress=lambda p, m: None, llm=None):
    import pdfplumber

    from . import pdf_docling

    ai = StructureAI(llm)
    slog = RepairLog()        # layout decisions; attached to the first table
    groups: list[Group] = []
    page_lines: list[list[str]] = []
    borderless = 0
    ocr_pages = 0
    use_model = pdf_docling.available()
    engine = config.PDF_ENGINE if use_model else "plumber"
    model_pages = 0
    reader_used = Counter()
    prev_above: list[str] | None = None
    tm: Counter = Counter()
    pool, futures = None, {}
    try:
        pdf = pdfplumber.open(str(path))
    except Exception as e:
        why = f"{type(e).__name__} {e!r}".lower()     # pdfplumber wraps pdfminer's PDFPasswordIncorrect
        if "password" in why or "encrypt" in why:
            raise ValueError("PDF is password-protected; remove the password and retry.")
        raise ValueError(f"Could not open PDF ({type(e).__name__}: {str(e)[:200]}).")
    pool_box: list = []
    with pdf, _shutdown_on_exit(pool_box):
        n_pages = len(pdf.pages)
        if use_model:
            progress(0.05, "Starting the table model on the unclear pages")
            pool, futures = _prefetch_model(path, pdf, engine, n_pages, tm)
            if pool is not None:
                pool_box.append(pool)
        for pi, raw in enumerate(pdf.pages):
            progress(0.05 + 0.4 * pi / max(1, n_pages), f"Reading PDF page {pi + 1}/{n_pages}")
            # 'bold' printed as the same glyph twice, slightly offset; diagonal watermark text
            page = raw.dedupe_chars().filter(_not_watermark)
            with _timed(tm, "geometry"):
                found = [] if engine == "docling" else [t for t in (_trim(t, slog) for t in _page_tables(page, pi)) if t]
                if found:
                    found = _better_than_rules(found, page, pi, slog)
            last = groups[-1] if groups else None
            running = last.first.vlines if last and last.last_page == pi - 1 else None
            if not found and running:
                # a vision-read table continues: cut this page at the same column boundaries
                found = [t for t in (_trim(t, slog) for t in _explicit_tables(page, pi, running)) if t]
                if found:
                    reader_used["vision"] += 1
            if not found:
                if engine != "docling":
                    found = [t for t in (_trim(t, slog) for t in _stream_tables(page, pi)) if t
                             and _looks_tabular([t.row(r, 0) for r in range(len(t.grid))])]
                # no ruling lines: cell boundaries are guessed from text alignment, so the table
                # model reads the page too and the better reading wins
                if use_model and model_pages < config.DOCLING_MAX_PAGES and (found or _numeric_page(page)):
                    model_pages += 1
                    progress(0.05 + 0.4 * pi / max(1, n_pages), f"Reading table structure on page {pi + 1}/{n_pages}")
                    try:
                        with _timed(tm, "table model"):
                            fut = futures.pop(pi, None)
                            raw_tables = fut.result() if fut is not None else None
                            alt = [t for t in (_trim(t, slog) for t in _docling_tables(path, pi, raw_tables)) if t]
                    except Exception as e:  # the geometry reading stays
                        alt = []
                        slog.note("Table model failed on page {p}: {e}", p=pi + 1, e=f"{type(e).__name__}"[:80])
                    found, chosen = _choose(found, alt, ai, slog, pi + 1)
                    reader_used[chosen] += 1
                # still unclear (no header on top, word fragments, glued values): the vision
                # model looks at the page once to lay out the columns of this table
                # a table the table model read well needs no second opinion from the (slow) vision model
                sure = bool(found) and found[0].reader == "docling" and _score(found) >= 0.7
                if (found or _numeric_page(page)) and not sure and _doubtful(found) and ai.enabled:
                    progress(0.05 + 0.4 * pi / max(1, n_pages), f"AI is reading the table layout on page {pi + 1}")
                    with _timed(tm, "AI vision"):
                        vt = _vision_table(page, pi, ai, slog)
                    # its cells were checked against the page text and its boundaries against every
                    # line it read, so it wins over a doubtful alignment guess
                    if vt is not None:
                        found = [vt]
                        reader_used["vision"] += 1
                if found and found[0].reader == "text":
                    borderless += len(found)
            above = _lines_above(page, found[0].top) if found else None
            for ti, pt in enumerate(found):
                g = groups[-1] if groups else None
                caption = _caption(above, prev_above) if ti == 0 else None
                if caption and g and g.last_page == pi - 1:
                    slog.note("Page {p} starts a new table (title above it: {t}).", p=pi + 1, t=caption[:120])
                elif g and ti == 0 and g.last_page == pi - 1 and len(g.parts) == 1 and _side_by_side(g.first, pt):
                    _join_columns(g.first, pt)
                    g.last_page = pi
                    slog.note("Page {p} continues the table's columns (same rows); joined side by side.", p=pi + 1)
                    continue
                elif g and ti == 0 and g.last_page == pi - 1 and g.parts[-1][0].page == pi - 1:
                    mapping = _align(g.first.cols, pt.cols)
                    if mapping is not None:
                        rows = [_remap(pt.row(r, LOOK), mapping, g.width) for r in range(min(LOOK, len(pt.grid)))]
                        skip = _leading_header_rows(g, rows, ai, slog, pi + 1)
                        if skip is not None:
                            g.repeat_depth = max(g.repeat_depth, skip)
                            g.stats["repeated_header"] += skip
                            identity = mapping == list(range(g.width))
                            g.stats["columns_realigned"] += 0 if identity else 1
                            g.parts.append((pt, None if identity else mapping, skip))
                            g.last_page = pi
                            continue
                groups.append(Group(pt))
            prev_above = above
            text = page.extract_text() or ""
            if len(text.strip()) < 20 and not found:
                try:
                    text = _ocr_page(page)
                    ocr_pages += 1
                except Exception:
                    pass
            page_lines.append([norm_text(l) for l in text.splitlines()])
            raw.flush_cache()
            cancel.check()

    if tm:
        slog.note("PDF read time: {t}.", t=", ".join(f"{k} {v:.0f}s" for k, v in tm.most_common()))
    if reader_used["docling"]:
        slog.note("{n} page(s) read with the TableFormer table-structure model (Docling).", n=reader_used["docling"])
    if reader_used["vision"]:
        slog.note("Column layout of {n} page(s) worked out by the AI vision model; values come from the PDF text.",
                  n=reader_used["vision"])
    if use_model and model_pages >= config.DOCLING_MAX_PAGES:
        slog.note("The table-structure model was used on the first {n} unclear pages only.", n=model_pages)
    tables = []
    for gi, g in enumerate(groups):
        cancel.check()
        log = slog if gi == 0 else RepairLog()
        if gi == 0 and borderless:
            log.note("{n} borderless table(s) detected from text alignment.", n=borderless)
        t = _group_to_table(g, gi, ai, log, work_dir, name)
        if t is not None and t.n_rows:
            tables.append(t)
    # framed one-row boxes next to real tables: signature blocks, letterheads, stamps
    boxes = [t for t in tables if t.n_rows < 2]
    if boxes and len(boxes) < len(tables):
        tables = [t for t in tables if t.n_rows >= 2]
        for i, t in enumerate(tables):
            t.name = re.sub(r"^table_\d+", f"table_{i + 1}", t.name)
        tables[0].repairs.note("{n} single-row box(es) (signature blocks, letterheads) skipped.", n=len(boxes))
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
    tables = [t for t in ingest_delimited(text_path, work_dir, f"{name}_text", progress) if _tabular_text(t)]
    if not tables:
        raise ValueError("No table found in this PDF: it holds running text (letters, articles, forms), "
                         "not rows and columns.")
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
