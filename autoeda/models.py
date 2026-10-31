"""Shared data structures."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPAIR_LABELS = {
    "preamble_skipped": "Title / preamble lines skipped before the header",
    "blank_line": "Blank lines skipped",
    "empty_row": "Rows with no values skipped",
    "repeated_header": "Repeated header rows removed",
    "trailing_delimiter": "Trailing delimiters trimmed",
    "unquoted_delimiter_merged": "Unquoted delimiter inside a value: fields re-merged",
    "overflow_joined": "Too many fields: overflow joined into last column",
    "merged_rows_split": "Several records on one line: split into rows",
    "mixed_delimiter": "Row used a different delimiter: re-split",
    "merged_fields_split": "Merged fields (missing delimiter) split apart",
    "missing_fields_padded": "Too few fields: missing values inserted",
    "sliding_row": "Row shifted by one column: realigned",
    "unbalanced_quote": "Unbalanced quote: quotes treated literally",
    "header_padded": "Header shorter than data: names generated",
    "header_truncated": "Header longer than data: extra names dropped",
    "multirow_header_combined": "Multi-row header combined into one",
    "merged_cells_filled": "Merged cells filled with their value",
    "encoding_fallback": "Non UTF-8 encoding detected and converted",
    "empty_column_dropped": "Empty spacer columns dropped",
    "total_row_removed": "Total / subtotal rows removed from the data",
    "note_row_skipped": "Footnote / note rows skipped",
    "section_row_to_column": "Section label rows turned into a column",
    "wrapped_row_merged": "Record wrapped over several lines: re-joined",
    "uncached_formulas": "Formulas without saved results (values unavailable)",
    "page_furniture_removed": "Repeated page headers/footers & page numbers removed",
    "comment_line": "Comment lines skipped",
    "nul_bytes_removed": "NUL bytes removed",
    "ocr_page": "Pages read with OCR (scanned images)",
    "columns_realigned": "Columns lined up by position across pages",
    "ai_layout": "Table layout decided with AI help",
}


def msg(tpl: str, **params) -> dict:
    """A translatable message: English template, values, and the English text."""
    try:
        text = tpl.format(**params)
    except (KeyError, IndexError, ValueError):
        text = tpl
    return {"tpl": tpl, "p": params, "text": text}


def _clip(v, n: int = 160):
    if v is None:
        return None
    s = v if isinstance(v, str) else repr(v)
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class RepairLog:
    counts: Counter = field(default_factory=Counter)
    examples: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)   # rows where the repair had near-equal options

    def add_ambiguous(self, line, raw, fields, chosen):
        if len(self.ambiguous) < 40 and raw:
            self.ambiguous.append({"line": line, "raw": _clip(raw, 400), "fields": fields, "chosen": chosen})

    def add(self, kind: str, line=None, before=None, after=None, detail: str | None = None):
        self.counts[kind] += 1
        ex = self.examples.setdefault(kind, [])
        if len(ex) < 5:
            ex.append({"line": line, "before": _clip(before), "after": _clip(after), "detail": detail})

    def note(self, tpl, **params):
        """Add a note: an English template + its values (translated at render time),
        or an already-built message dict."""
        m = tpl if isinstance(tpl, dict) else msg(tpl, **params)
        if m not in self.notes:
            self.notes.append(m)

    def to_dict(self) -> dict:
        return {
            "total": sum(self.counts.values()),
            "items": [
                {"kind": k, "label": REPAIR_LABELS.get(k, k), "count": c, "examples": self.examples.get(k, [])}
                for k, c in self.counts.most_common()
            ],
            "notes": self.notes,
            "ambiguous": self.ambiguous,
        }


@dataclass
class Table:
    name: str
    path: Path            # parquet file (or original parquet) holding the data
    columns: list[str]
    extra_paths: list = field(default_factory=list)  # more parquet parts (e.g. repaired rejects)
    drop_columns: list = field(default_factory=list)  # helper columns present in parquet but not profiled
    n_rows: int = 0
    source: dict = field(default_factory=dict)
    repairs: RepairLog = field(default_factory=RepairLog)


def clean_header(names: list, width: int | None = None) -> list[str]:
    """Normalise header names: strip, fill blanks, de-duplicate."""
    width = width or len(names)
    out, seen = [], Counter()
    for i in range(width):
        raw = names[i] if i < len(names) else None
        name = " ".join(str(raw).replace("﻿", "").strip().strip('"').split()) if raw is not None else ""
        if not name:
            name = f"column_{i + 1}"
        base = name
        seen[base.lower()] += 1
        if seen[base.lower()] > 1:
            name = f"{base}_{seen[base.lower()]}"
        out.append(name)
    return out
