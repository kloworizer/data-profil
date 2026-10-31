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

import os
import re
from collections import Counter
from pathlib import Path

from .. import cancel, config
from ..models import RepairLog
from .delimited import ingest_delimited
from .grid import cell_str, grid_to_table, norm_text
from .repair import learn_classes, vclass
from .structure import StructureAI, combine_header, header_depth_guess, label_only, nn, row_sim, texty

TEXT_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text", "snap_tolerance": 3,
                 "join_tolerance": 3, "min_words_vertical": 3, "min_words_horizontal": 1}
PAGE_NO_RE = re.compile(r"^\s*[-–]?\s*(page|hal(aman)?|halaman|p\.|pg\.?|seite)?\s*\d+\s*((of|dari|von|/)\s*\d+)?\s*[-–]?\s*$", re.I)
HEADER_MATCH = 0.85      # row similarity above which a row is the repeated header
HEADER_MAYBE = 0.5       # between the two: ask the AI
MAX_HEADER_ROWS = 4
LOOK = MAX_HEADER_ROWS + 2
EPS = 0.5


# --- one page table ---------------------------------------------------------------------
def _clean(v) -> str:
    return norm_text(cell_str(v)).replace("\n", " ").strip()


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
                out.append("" if r in self.labels else self.text(*o))   # horizontal span in the body
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
        pt = PageTable(pi, grid, origin, cols, settings is None, "lines" if settings is None else "text")
        _mark_labels(pt)
        out.append(pt)
    return out


def _docling_tables(path: Path, pi: int) -> list[PageTable]:
    from . import pdf_docling

    def make(grid, origin, cols, header_rows, sections):
        pt = PageTable(pi, grid, origin, cols, True, "docling")
        pt.model_header = header_rows
        _mark_labels(pt)
        pt.labels |= {r for r in sections if r >= header_rows}
        return pt
    return pdf_docling.page_tables(path, pi, make)


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
    page.to_image(resolution=110).original.save(buf, "PNG")
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
    numeric = sum(1 for c in filled if re.search(r"\d", c)) / len(filled)
    return avg_len < 40 and numeric >= 0.15


def _ocr_page(page) -> str:
    import pytesseract
    img = page.to_image(resolution=300).original
    return pytesseract.image_to_string(img, lang=os.getenv("AUTOEDA_OCR_LANG", "eng"))


# --- stitching across pages ------------------------------------------------------------
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
    try:
        pdf = pdfplumber.open(str(path))
    except Exception as e:
        if "password" in str(e).lower() or "encrypt" in type(e).__name__.lower() or "Password" in type(e).__name__:
            raise ValueError("PDF is password-protected; remove the password and retry.")
        raise ValueError(f"Could not open PDF ({type(e).__name__}: {str(e)[:200]}).")
    with pdf:
        n_pages = len(pdf.pages)
        for pi, page in enumerate(pdf.pages):
            progress(0.05 + 0.4 * pi / max(1, n_pages), f"Reading PDF page {pi + 1}/{n_pages}")
            found = [] if engine == "docling" else [t for t in (_trim(t, slog) for t in _page_tables(page, pi)) if t]
            last = groups[-1] if groups else None
            running = last.first.vlines if last and last.last_page == pi - 1 else None
            if not found and running:
                # a vision-read table continues: cut this page at the same column boundaries
                found = [t for t in (_trim(t, slog) for t in _explicit_tables(page, pi, running)) if t]
                if found:
                    reader_used["vision"] += 1
            if not found:
                if engine != "docling":
                    found = [t for t in (_trim(t, slog) for t in _page_tables(page, pi, TEXT_SETTINGS)) if t
                             and _looks_tabular([t.row(r, 0) for r in range(len(t.grid))])]
                # no ruling lines: cell boundaries are guessed from text alignment, so the table
                # model reads the page too and the better reading wins
                if use_model and model_pages < config.DOCLING_MAX_PAGES and (found or _numeric_page(page)):
                    model_pages += 1
                    progress(0.05 + 0.4 * pi / max(1, n_pages), f"Reading table structure on page {pi + 1}/{n_pages}")
                    try:
                        alt = [t for t in (_trim(t, slog) for t in _docling_tables(path, pi)) if t]
                    except Exception as e:  # the geometry reading stays
                        alt = []
                        slog.note("Table model failed on page {p}: {e}", p=pi + 1, e=f"{type(e).__name__}"[:80])
                    found, chosen = _choose(found, alt, ai, slog, pi + 1)
                    reader_used[chosen] += 1
                # still unclear (no header on top, word fragments, glued values): the vision
                # model looks at the page once to lay out the columns of this table
                if (found or _numeric_page(page)) and _doubtful(found) and ai.enabled:
                    progress(0.05 + 0.4 * pi / max(1, n_pages), f"AI is reading the table layout on page {pi + 1}")
                    vt = _vision_table(page, pi, ai, slog)
                    # its cells were checked against the page text and its boundaries against every
                    # line it read, so it wins over a doubtful alignment guess
                    if vt is not None:
                        found = [vt]
                        reader_used["vision"] += 1
                if found and found[0].reader == "text":
                    borderless += len(found)
            for ti, pt in enumerate(found):
                g = groups[-1] if groups else None
                if g and ti == 0 and g.last_page == pi - 1 and g.parts[-1][0].page == pi - 1:
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
            text = page.extract_text() or ""
            if len(text.strip()) < 20 and not found:
                try:
                    text = _ocr_page(page)
                    ocr_pages += 1
                except Exception:
                    pass
            page_lines.append([norm_text(l) for l in text.splitlines()])
            page.flush_cache()
            cancel.check()

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
