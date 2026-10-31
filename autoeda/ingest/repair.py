"""Row-level repair of structurally broken records.

The repairer learns the expected "shape" of a table from clean sample rows
(field count + coarse type class per column) and then fixes each incoming row
whose structure deviates, choosing the candidate fix that best fits the
learned column types:

* too many fields   -> unquoted delimiter inside a value (re-merge), several
                       records on one line (split), trailing delimiters
* too few fields    -> other delimiter used on that row, merged fields
                       (missing delimiter), missing values
* right count, but values in the wrong columns -> sliding / shifted row
"""
from __future__ import annotations

import re
from collections import Counter
from itertools import combinations

from ..models import RepairLog

NUM_RE = re.compile(r"^[+-]?[$€£¥]?\s?\(?\d[\d,. ']*\)?\s?%?$")
DATE_RE = re.compile(
    r"^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2,4})"
    r"([ T]\d{1,2}:\d{2}(:\d{2})?.*)?$"
)


def vclass(v) -> str:
    """Coarse class of a raw value: E(mpty), I(nteger), N(on-integer number), D(ate), T(ext)."""
    if v is None:
        return "E"
    s = v.strip()
    if not s:
        return "E"
    c = s[0]
    if c.isdigit() or (c in "+-$€£(" and len(s) > 1 and s[1:2].isdigit()):
        if s.isdigit():
            return "I"
        if DATE_RE.match(s):
            return "D"
        if NUM_RE.match(s):
            return "N"
    return "T"


PERIOD_RE = re.compile(r"^((19|20)\d{2}([-/ ]?(0?[1-9]|1[0-2]|q[1-4]|h[12]))?|q[1-4]([ -]?(19|20)\d{2})?|"
                       r"(fy|ty)\s?\d{2,4})$", re.I)


def looks_like_data(candidate: list[str], body: list[list[str]]) -> bool:
    """True when a header candidate is really a data row: its values reappear
    in their own column, or are numbers/dates of the column's own type."""
    body = [r for r in body[:300] if r]
    if not body:
        return False
    width = len(candidate)
    classes = learn_classes(body, width)
    filled = hits = 0
    for i, cell in enumerate(candidate):
        cell = cell.strip()
        if not cell:
            continue
        filled += 1
        col_vals = {r[i].strip() for r in body if i < len(r)}
        cls = vclass(cell)
        if cell in col_vals:
            hits += 1
        elif cls in "IND" and not PERIOD_RE.match(cell) and (cls == classes[i] or {cls, classes[i]} == {"I", "N"}):
            hits += 1
    return filled > 0 and hits >= max(1, (filled + 1) // 2)


def learn_classes(rows: list[list[str]], width: int) -> list[str]:
    """Expected class per column: I/N/D/T when >=85% of non-empty values agree
    (integers count as numbers), E when the column is (almost) always empty,
    A(ny) otherwise."""
    counters = [Counter() for _ in range(width)]
    for r in rows:
        for i in range(min(width, len(r))):
            counters[i][vclass(r[i])] += 1
    out = []
    for c in counters:
        total = sum(c.values())
        filled = total - c["E"]
        if total == 0:
            out.append("A")
        elif filled <= 0.02 * total:
            out.append("E")
        else:
            num = c["I"] + c["N"]
            if num >= 0.85 * filled:
                out.append("I" if c["I"] >= 0.95 * num else "N")
                continue
            cls, k = max(((k, c[k]) for k in "DT"), key=lambda kv: kv[1])
            out.append(cls if k >= 0.85 * filled else "A")
    return out


class RowRepairer:
    def __init__(self, header: list[str], classes: list[str], delimiter: str,
                 log: RepairLog, alt_delimiters=(), split_fn=None, slide_check=True):
        self.header = header
        self.m = len(header)
        self.classes = classes
        self.delim = delimiter if delimiter not in ("WS", "WS2") else " "
        self.log = log
        self.alt = [d for d in alt_delimiters if d != delimiter]
        self.split_fn = split_fn
        self.header_norm = [h.strip().lower() for h in header]
        self.typed = [(i, c) for i, c in enumerate(classes) if c in "INDE"]
        self.slide_check = slide_check and sum(1 for _, c in self.typed if c in "IND") >= 2

    # --- scoring ---------------------------------------------------------
    def _cost(self, v, exp: str) -> float:
        if exp == "A":
            return 0.0
        c = vclass(v)
        if c == exp:
            return 0.0
        if c == "E":
            return 0.25
        if exp == "E":
            return 0.6
        if exp == "T":
            return 0.35
        if exp == "N" and c == "I":
            return 0.0
        if exp == "I" and c == "N":
            return 0.7
        return 1.0

    def score(self, row: list) -> float:
        cl = self.classes
        return sum(self._cost(row[i], cl[i]) for i in range(self.m))

    def _typed_score(self, row: list) -> float:
        return sum(self._cost(row[i], c) for i, c in self.typed)

    # --- main entry ------------------------------------------------------
    def fix(self, fields: list[str], line_no=None, raw: str | None = None) -> list[list[str]]:
        m, n = self.m, len(fields)
        if n == m:
            if self.slide_check:
                return [self._fix_slide(fields, line_no)]
            return [fields]
        if n == 1 and m > 1 and fields[0].strip().lower() == self.header_norm[0]:
            return [fields + [""] * (m - 1)]
        if n > m:
            return self._fix_long(fields, line_no, raw)
        if fields[0].lstrip().startswith(("#", "//")) and n <= max(1, m // 2):
            self.log.add("comment_line", line_no, raw or self._join(fields))
            return []
        return [self._fix_short(fields, line_no, raw)]

    def is_header_repeat(self, fields: list[str]) -> bool:
        if len(fields) != self.m:
            return False
        return [f.strip().lower() for f in fields] == self.header_norm

    def _fix_slide(self, fields, line_no):
        s = self._typed_score(fields)
        if s < 1.5:
            return fields
        s = self.score(fields)
        cands = []
        if fields[0].strip() == "":
            cands.append(fields[1:] + [""])
        if fields[-1].strip() == "":
            cands.append([""] + fields[:-1])
        best = min(cands, key=self.score, default=None)
        if best is not None and self.score(best) <= s - 1.0:
            self.log.add("sliding_row", line_no, self._join(fields), self._join(best))
            return best
        return fields

    def _fix_long(self, fields, line_no, raw):
        m, n = self.m, len(fields)
        if all(f.strip() == "" for f in fields[m:]):
            trimmed = fields[:m]
            if self._typed_score(trimmed) < 1.0:
                self.log.add("trailing_delimiter", line_no)
                return [trimmed]
            # empty tail but values misaligned: an unquoted delimiter is more likely
        # several records glued on one line
        if n % m == 0:
            chunks = [fields[i:i + m] for i in range(0, n, m)]
            if all(self.score(c) <= 0.5 + 0.1 * m for c in chunks) and not any(self.is_header_repeat(c) for c in chunks):
                self.log.add("merged_rows_split", line_no, raw, f"{len(chunks)} rows")
                return chunks
        k = n - m
        cands = []
        if k <= 4 and m <= 300:
            for i in range(m):
                cand = fields[:i] + [self.delim.join(fields[i:i + k + 1])] + fields[i + k + 1:]
                # prefer merging into text-like columns, then the right-most one
                bias = (0.0 if self.classes[i] in "TA" else 0.3) - i * 1e-4
                cands.append((self.score(cand) + bias, i, cand))
            if fields[0].strip() == "":
                cand = fields[1:]
                if len(cand) == m:
                    cands.append((self.score(cand) - 0.05, -1, cand))
        if cands:
            sc, i, best = min(cands, key=lambda t: t[0])
            self._note_ambiguity(cands, 0, 2, line_no, raw, fields, best)
            if i >= 0:
                self.log.add("unquoted_delimiter_merged", line_no, raw or self._join(fields), self._join(best),
                             detail=f"column '{self.header[i]}'")
            else:
                self.log.add("sliding_row", line_no, raw or self._join(fields), self._join(best))
            return [best]
        best = fields[:m - 1] + [self.delim.join(fields[m - 1:])]
        self.log.add("overflow_joined", line_no, raw, self._join(best))
        return [best]

    def _fix_short(self, fields, line_no, raw):
        m, n = self.m, len(fields)
        if raw is not None and self.split_fn is not None:
            for d in self.alt:
                if d in raw:
                    alt = self.split_fn(raw, d)
                    if len(alt) == m:
                        self.log.add("mixed_delimiter", line_no, raw, self._join(alt), detail=f"delimiter {d!r}")
                        return alt
        k = m - n
        cands = []  # (score, kind, row)
        if k <= 2 and m <= 120:
            for pos in combinations(range(m), k):
                row, it = [], iter(fields)
                for j in range(m):
                    row.append("" if j in pos else next(it))
                cands.append((self.score(row) + 0.01 * (m - 1 - pos[-1]) * 0.01, "missing_fields_padded", row))
        # merged fields: a value that is really k+1 values joined by whitespace / other separator
        for i, f in enumerate(fields):
            for sep in (None, ";", ",", "|", "/", "-"):
                parts = f.split(sep) if sep else f.split()
                if len(parts) == k + 1:
                    row = fields[:i] + [p.strip() for p in parts] + fields[i + 1:]
                    cands.append((self.score(row) - 0.3, "merged_fields_split", row))
        if not cands:
            row = fields + [""] * k
            self.log.add("missing_fields_padded", line_no, raw or self._join(fields), self._join(row))
            return row
        sc, kind, best = min(cands, key=lambda t: t[0])
        pad_end = fields + [""] * k
        if kind == "missing_fields_padded" and self.score(pad_end) <= sc + 1e-9:
            best = pad_end
        self._note_ambiguity(cands, 0, 2, line_no, raw, fields, best)
        self.log.add(kind, line_no, raw or self._join(fields), self._join(best))
        return best

    def _note_ambiguity(self, cands, si, ri, line_no, raw, fields, best, margin=0.15):
        """Remember rows where a different reading scored almost as well (AI second opinion)."""
        if len(cands) < 2 or not hasattr(self.log, "add_ambiguous"):
            return
        ranked = sorted(cands, key=lambda t: t[si])
        if ranked[1][si] - ranked[0][si] < margin and ranked[1][ri] != ranked[0][ri]:
            self.log.add_ambiguous(line_no, raw or self._join(fields), list(fields), list(best))

    def _join(self, row):
        return " ⎮ ".join("" if v is None else str(v) for v in row)
