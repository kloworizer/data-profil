"""Table structure helpers shared by the PDF and spreadsheet readers.

Rules first, AI second: the readers decide layout questions (how many header
rows, is this page's first row the repeated header, does a table continue on the
next page) from geometry, merged-cell spans and value types. When those signals
disagree or are missing, the local Ollama model is asked for a second opinion;
its answer is bounds-checked and logged as an AI decision in the report.
"""
from __future__ import annotations

import difflib
import json
import re

from .. import cancel
from .repair import PERIOD_RE, vclass

MAX_AI_CALLS = 12          # per file: layout questions are few and must stay cheap
PREVIEW_ROWS = 10
PREVIEW_COLS = 16


# --- comparing rows ----------------------------------------------------------------
def norm_cell(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def cell_sim(a: str, b: str) -> float:
    a, b = norm_cell(a), norm_cell(b)
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def row_sim(a: list, b: list) -> float:
    """Share of columns where the two rows hold (nearly) the same text; columns
    empty in both rows are ignored."""
    n = max(len(a), len(b))
    score = used = 0
    for i in range(n):
        x = a[i] if i < len(a) else ""
        y = b[i] if i < len(b) else ""
        if not norm_cell(x) and not norm_cell(y):
            continue
        used += 1
        s = cell_sim(x, y)
        score += 1.0 if s >= 0.85 else s * 0.5
    return score / used if used else 0.0


def nn(row) -> int:
    return sum(1 for c in row if str(c or "").strip())


def texty(row) -> float:
    filled = [str(c).strip() for c in row if str(c or "").strip()]
    if not filled:
        return 0.0
    return sum(1 for c in filled if vclass(c) == "T") / len(filled)


def period_row(row) -> bool:
    """A row of period names only (2023, 2024, Q1 ...): typical second header level."""
    filled = [str(c).strip() for c in row if str(c or "").strip()]
    periods = [c for c in filled if PERIOD_RE.match(c)]
    return len(periods) >= 2 and all(PERIOD_RE.match(c) or vclass(c) == "T" for c in filled)


def label_only(row) -> bool:
    """Header-ish row: labels or period names, at least two cells."""
    filled = [str(c).strip() for c in row if str(c or "").strip()]
    return len(filled) >= 2 and all(vclass(c) == "T" or PERIOD_RE.match(c) for c in filled)


def header_depth_guess(rows: list[list[str]], start: int = 1, limit: int = 4) -> tuple[int, bool]:
    """Rule-based header depth given that rows[:start] are header rows.
    Returns (depth, uncertain). A following row is another header level when it
    holds labels / period names only and real data (numbers) follows below it."""
    depth = start
    uncertain = False
    while depth < min(limit, len(rows) - 1):
        nxt = rows[depth]
        below = [r for r in rows[depth + 1:depth + 6] if nn(r) >= 2]
        if not below or not label_only(nxt):
            break
        data_below = any(vclass(str(c)) in "IND" and not PERIOD_RE.match(str(c).strip())
                         for r in below for c in r if str(c or "").strip())
        if not data_below:
            break
        # values that reappear further down in the same column are data, not labels
        repeats = sum(1 for i, c in enumerate(nxt) if norm_cell(c) and any(
            i < len(r) and norm_cell(r[i]) == norm_cell(c) for r in below))
        if repeats:
            break
        strong = period_row(nxt) or any(not norm_cell(c) for c in nxt[:1])
        if not strong:
            uncertain = True
            break
        depth += 1
    return depth, uncertain


def combine_header(band: list[list[str]], carry: bool = False) -> list[str]:
    """Multi-level header rows -> one name per column ('Sales / 2023').
    carry: a blank upper label inherits the label on its left (borderless tables
    where a group label is printed once above several columns)."""
    w = max(len(r) for r in band)
    rows = [[str(r[i]).strip() if i < len(r) and r[i] is not None else "" for i in range(w)] for r in band]
    if carry and len(rows) > 1:
        for k in range(len(rows) - 1):
            for i in range(1, w):
                if not rows[k][i] and rows[k][i - 1] and rows[k + 1][i] and rows[k + 1][i - 1]:
                    rows[k][i] = rows[k][i - 1]
    out = []
    for i in range(w):
        parts = []
        for r in rows:
            v = r[i]
            if v and (not parts or norm_cell(parts[-1]) != norm_cell(v)):
                parts.append(v)
        out.append(" / ".join(parts))
    return out


# --- AI second opinion ----------------------------------------------------------------
def _preview(rows: list[list], n: int = PREVIEW_ROWS) -> list[list[str]]:
    return [[str(c or "")[:40] for c in r[:PREVIEW_COLS]] for r in rows[:n]]


def _lines(rows) -> str:
    return "\n".join(f"row {i + 1}: {json.dumps(r, ensure_ascii=False)}" for i, r in enumerate(_preview(rows)))


class StructureAI:
    """Asks the local model about table layout. Every method returns None when
    the model is unavailable, over budget, or answers something unusable; the
    caller then keeps its rule-based decision."""

    def __init__(self, llm=None, max_calls: int = MAX_AI_CALLS):
        self.llm = llm
        self.max_calls = max_calls
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return self.llm is not None and self.calls < self.max_calls

    def _ask(self, prompt: str) -> dict | None:
        if not self.enabled:
            return None
        cancel.check()
        self.calls += 1
        from ..llm import _parse_json
        try:
            data = _parse_json(self.llm.chat(prompt, temperature=0))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def header_rows(self, rows: list[list[str]], guess: int) -> int | None:
        """How many top rows are column headings."""
        top = min(4, len(rows) - 1)
        if top < 1:
            return None
        data = self._ask(
            "These are the first rows of a table read from a document (one JSON array per row, \"\" = empty "
            "cell; a merged cell may repeat its text in each column it covers):\n" + _lines(rows) + "\n\n"
            "How many of the top rows are COLUMN HEADINGS rather than data records? Headings can span several "
            "rows, e.g. a group name ('Sales') above period names ('2023', '2024') or units. A row of years or "
            "quarters directly under a group name is a heading row. Data records hold the actual values.\n"
            f'Return JSON: {{"header_rows": <number from 1 to {top}>, "reason": "<short reason>"}}')
        try:
            n = int((data or {}).get("header_rows"))
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= top else None

    def is_repeated_header(self, header: list[list[str]], row: list[str]) -> bool | None:
        """Is `row` (first row on a new page) a re-print of the column headings?"""
        # side by side with the closest heading row: small models compare pairs far better than tables
        # and only about the cells the rules can't settle: identical or clearly different
        # cells are decided here, so a weak answer moves the verdict at most by the unclear cells
        ref = max(header, key=lambda h: row_sim(row, h))
        cols = [(str(h or ""), str(v or "")) for h, v in zip(ref, row) if norm_cell(h) or norm_cell(v)]
        if not cols:
            return None
        same = sum(1 for h, v in cols if norm_cell(h) == norm_cell(v))
        unsure = [{"a": h[:40], "b": v[:40]} for h, v in cols
                  if norm_cell(h) != norm_cell(v) and cell_sim(h, v) >= 0.5][:PREVIEW_COLS]
        if unsure:
            data = self._ask(
                "Text read from a PDF can contain reading errors (missing, extra or changed letters, "
                "abbreviations).\nPairs of short texts (JSON):\n"
                f"{json.dumps(unsure, ensure_ascii=False)}\n\n"
                "For EACH pair, in the same order: true if a and b are the same word(s) despite reading errors, "
                'false if they are different words.\nReturn JSON: {"same": [true or false, ...]}')
            v = (data or {}).get("same")
            if not isinstance(v, list) or len(v) != len(unsure) or not all(isinstance(x, bool) for x in v):
                return None
            same += sum(v)
        return 3 * same >= 2 * len(cols)

    def continues(self, header: list[list[str]], last_rows: list[list[str]], next_rows: list[list[str]]) -> bool | None:
        """Is next_rows (top of the next page) more records of the same table?"""
        data = self._ask(
            "A table on one page has these column headings:\n" + _lines(header) + "\n"
            "and ends with these rows:\n" + _lines(last_rows[-3:]) + "\n\n"
            "The next page starts with a table whose first rows are:\n" + _lines(next_rows[:5]) + "\n\n"
            "Is the table on the next page the SAME table continuing (more records with the same columns), "
            "or a DIFFERENT table with its own headings?\n"
            'Return JSON: {"same_table": true or false}')
        v = (data or {}).get("same_table")
        return v if isinstance(v, bool) else None

    def vision_layout(self, image_b64: str, words: set[str]) -> dict | None:
        """Read the table on a page image: column names and the first records.
        Only text that really is on the page survives (every token is checked against
        the PDF's own words), so the result can steer the column boundaries but never
        put an invented value into the data."""
        if not self.enabled or not hasattr(self.llm, "has_vision") or not self.llm.has_vision():
            return None
        cancel.check()
        self.calls += 1
        from ..llm import _parse_json
        try:
            data = _parse_json(self.llm.chat(
                "Transcribe the table in this image.\n"
                'Return JSON: {"columns": ["<name of column 1>", ...], "rows": ["<cell> | <cell> | ...", ...]}\n'
                "columns: one name per column of values. When a group heading spans several columns, combine it "
                "with each sub-heading, e.g. \"Sales 2023\", \"Sales 2024\".\n"
                "rows: the first 8 data records, each ONE string with its cells separated by \" | \" (one cell per "
                "column, nothing between two bars for an empty cell). Copy the text exactly; keep a cell's words "
                "together.",
                temperature=0, images=[image_b64]))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        cols = data.get("columns")
        if not isinstance(cols, list) or len(cols) < 2 or not all(isinstance(c, str) for c in cols):
            return None
        w = len(cols)
        known = {norm_cell(x) for x in words}
        seen = lambda s: all(norm_cell(t) in known for t in str(s).split())  # noqa: E731
        # small models sometimes run all rows together; the cells are what matters:
        # they tell which words on a line belong to one value
        cells = set()
        for r in data.get("rows") or []:
            for c in (r.split("|") if isinstance(r, str) else r if isinstance(r, list) else []):
                c = " ".join(str(c or "").split())
                if c and seen(c):
                    cells.add(c)
        if len(cells) < w:
            return None
        names_ok = sum(1 for c in cols if c.strip() and seen(c)) >= 0.7 * w
        return {"columns": [" ".join(c.split()) for c in cols] if names_ok else None, "width": w, "cells": cells}

    def pick_reading(self, a: list[list[str]], b: list[list[str]]) -> int | None:
        """Two readers split the same page into cells differently: 0 = reading A, 1 = B."""
        data = self._ask(
            "The same table on a PDF page was split into cells by two different readers.\n"
            "Reading A:\n" + _lines(a) + "\n\nReading B:\n" + _lines(b) + "\n\n"
            "Which reading puts each value in its own correct column? Signs of a wrong reading: two values in one "
            "cell (e.g. '41 2024-05-14'), a heading split over two columns, values shifted into the wrong column.\n"
            'Return JSON: {"better": "A" or "B"}')
        v = str((data or {}).get("better", "")).strip().upper()
        return {"A": 0, "B": 1}.get(v)
