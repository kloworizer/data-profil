"""Robust ingestion of delimited / plain text files (csv, tsv, txt, dat, gz ...).

Pipeline: sniff encoding -> sniff delimiter -> locate header (skip preamble)
-> learn column shape from clean rows -> try the fast DuckDB reader when the
sample is clean -> otherwise stream every line through the RowRepairer into
Parquet.
"""
from __future__ import annotations

import codecs
import csv
import gzip
from functools import partial
import re
from collections import Counter, deque
from pathlib import Path

from .. import config
from ..models import RepairLog, Table, clean_header
from .repair import RowRepairer, learn_classes, looks_like_data, vclass

CANDIDATES = [",", ";", "\t", "|"]
EXTRA = [":", "~", "^"]
QUOTE = '"'
WS2_RE = re.compile(r"\s{2,}|\t")
WS_RE = re.compile(r"\s+")
SEP_LINE_RE = re.compile(r"^\s*sep=(.)\s*$", re.I)   # Excel's delimiter hint line
csv.field_size_limit(2**31 - 1)


# --- low level helpers -------------------------------------------------------
def _open_binary(path: Path):
    return gzip.open(path, "rb") if path.suffix.lower() == ".gz" else open(path, "rb")


def open_text(path: Path, encoding: str):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding=encoding, errors="replace", newline="")
    return open(path, "r", encoding=encoding, errors="replace", newline="")


def detect_encoding(path: Path) -> str:
    with _open_binary(path) as f:
        raw = f.read(2_000_000)
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if len(raw) > 64 and raw.count(b"\x00") >= 0.4 * len(raw):   # UTF-16 without BOM
        return "utf-16-le" if raw[1::2].count(b"\x00") > raw[0::2].count(b"\x00") else "utf-16-be"
    cut = raw.rfind(b"\n")
    try:
        (raw[:cut] if cut > 0 else raw).decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw[:300_000]).best()
        if best and best.encoding:
            return best.encoding
    except Exception:
        pass
    return "latin-1"


def split_line(line: str, delim: str, esc: str | None = None) -> list[str]:
    if delim == "WS2":
        return WS2_RE.split(line.strip())
    if delim == "WS":
        return WS_RE.split(line.strip())
    if QUOTE not in line:
        return line.split(delim)
    try:
        return next(csv.reader(line.splitlines(True) or [""], delimiter=delim, quotechar=QUOTE,
                               escapechar=esc, doublequote=esc is None))
    except (csv.Error, StopIteration):
        return line.replace(QUOTE, "").split(delim)


def _quotes(line: str, esc: str | None) -> int:
    n = line.count(QUOTE)
    return n - line.count(esc + QUOTE) if esc else n


def logical_lines(f, max_join: int = 50, esc: str | None = None):
    """Yield (line_no, text, broken_quote). Joins physical lines while a quoted
    field is open; gives up after `max_join` lines and flags the quote broken.
    NUL bytes are stripped."""
    lineno = 0
    pushback: deque = deque()

    def read():
        nonlocal lineno
        if pushback:
            return pushback.popleft()
        raw = f.readline()
        if not raw:
            return None
        lineno += 1
        line = raw.rstrip("\r\n")
        return lineno, line.replace("\x00", "") if "\x00" in line else line

    while True:
        item = read()
        if item is None:
            return
        ln, line = item
        q = _quotes(line, esc)
        if q % 2 == 0:
            yield ln, line, False
            continue
        parts, consumed, ok = [line], [], False
        while len(consumed) < max_join:
            nx = read()
            if nx is None:
                break
            consumed.append(nx)
            parts.append(nx[1])
            q += _quotes(nx[1], esc)
            if q % 2 == 0:
                ok = True
                break
        if ok:
            yield ln, "\n".join(parts), False
        else:
            pushback.extendleft(reversed(consumed))
            yield ln, line, True


def detect_delimiter(lines: list[str]) -> tuple[str, int, float]:
    """Return (delimiter, field count, consistency) — the delimiter whose field
    count is most stable across lines (standard delimiters preferred)."""
    best, best_score = (None, 1, 0.0), 0.0
    for d in CANDIDATES + EXTRA:
        counts = [len(split_line(l, d)) for l in lines]
        mode, freq = Counter(counts).most_common(1)[0]
        if mode < 2:
            continue
        consistency = freq / len(lines)
        score = consistency * (1.0 if d in CANDIDATES else 0.7) + 0.005 * min(mode, 30)
        if score > best_score:
            best, best_score = (d, mode, consistency), score
    if best[0] is None or best_score < 0.5:
        for d in ("WS2", "WS"):
            counts = [len(split_line(l, d)) for l in lines]
            mode, freq = Counter(counts).most_common(1)[0]
            score = freq / len(lines) * 0.8
            if mode >= 2 and score > best_score:
                best, best_score = (d, mode, freq / len(lines)), score
    return best


def _texty(fields: list[str]) -> float:
    """Share of fields that read as labels (period headers like 2019 count)."""
    from .grid import _texty as grid_texty
    filled = [f for f in fields if f.strip()]
    if not filled:
        return 0.0
    return grid_texty(fields) * len(filled) / len(fields)


def split_single(s: str, d=None) -> list[str]:
    return [s]


def make_plan(sample: list[tuple], log: RepairLog) -> dict:
    """Sniff delimiter, header, preamble and column shape from sample lines
    [(line_no, text, broken), ...]. Shared by the local and the Spark engine."""
    texts = [t for _, t, _ in sample]
    sep = SEP_LINE_RE.match(texts[0])
    if sep and len(texts) > 1:
        d = sep.group(1)
        mode, freq = Counter(len(split_line(t, d)) for t in texts[1:]).most_common(1)[0]
        delim, m, conf = d, mode, freq / (len(texts) - 1)
        log.note("Delimiter declared by an Excel 'sep={d}' line.", d=d)
    else:
        delim, m, conf = detect_delimiter(texts)
    if delim is None:
        delim, m = "\n", 1   # single column of free text
        log.note("No delimiter detected: treated as a single-column text file.")
    else:
        shown = {"\t": "TAB", "WS2": "2+ spaces (fixed width)", "WS": "whitespace"}.get(delim, delim)
        log.note("Delimiter detected: {d} with {m} columns (consistency {c:.0%}).", d=repr(shown), m=m, c=conf)
    # backslash-escaped quotes (\" as in MySQL dumps) instead of doubled quotes
    esc = "\\" if sum(t.count('\\"') for t in texts) >= 3 else None
    if esc:
        log.note('Backslash-escaped quotes (\\") detected.')
    split = split_single if m == 1 else partial(split_line, esc=esc)
    parsed = [split(t, delim) for t in texts]

    first = next((i for i, p in enumerate(parsed[:200]) if len(p) == m), 0)
    hdr_idx = first
    if first > 0 and len(parsed[first - 1]) >= 2 and abs(len(parsed[first - 1]) - m) <= 3 and _texty(parsed[first - 1]) >= 0.6:
        hdr_idx = first - 1
    has_header = (m == 1 and _texty(parsed[hdr_idx]) >= 0.5 and len(texts[hdr_idx]) < 60) or \
        (m > 1 and _texty(parsed[hdr_idx]) >= 0.6
         and not looks_like_data(parsed[hdr_idx], [p for p in parsed[hdr_idx + 1:] if len(p) == m]))
    for i in range(hdr_idx):
        log.add("preamble_skipped", sample[i][0], texts[i])

    data_rows = [p for p in parsed[hdr_idx + (1 if has_header else 0):] if len(p) == m]
    if has_header:
        hfields = parsed[hdr_idx]
        if len(hfields) < m:
            log.add("header_padded", sample[hdr_idx][0], texts[hdr_idx], detail=f"{len(hfields)} names for {m} columns")
        elif len(hfields) > m:
            extra = [h for h in hfields[m:] if h.strip()]
            if extra:
                log.add("header_truncated", sample[hdr_idx][0], texts[hdr_idx], detail=f"dropped {extra}")
        header = clean_header(hfields[:m], m)
    else:
        header = clean_header([], m) if m > 1 else ["text"]
        log.note("No header row detected: generic column names generated.")
    return {
        "delim": delim, "m": m, "split": split, "esc": esc, "header": header, "has_header": has_header,
        "hdr_idx": hdr_idx, "header_ln": sample[hdr_idx][0] if has_header else sample[hdr_idx][0] - 1,
        "classes": learn_classes(data_rows[:1000], m),
        "skip_texts": texts[:hdr_idx + (1 if has_header else 0)],
    }


def plan_repairer(plan: dict, log: RepairLog) -> RowRepairer:
    alts = [d for d in CANDIDATES if d != plan["delim"]]
    return RowRepairer(plan["header"], plan["classes"], plan["delim"], log, alt_delimiters=alts,
                       split_fn=partial(split_line, esc=plan.get("esc")), slide_check=config.SLIDE_CHECK)


# --- main entry --------------------------------------------------------------
def ingest_delimited(path: Path, work_dir: Path, name: str, progress=lambda p, m: None,
                     max_rows: int = 0) -> list[Table]:
    from .sink import ParquetSink   # lazy: executors (Spark) only need the stdlib parts of this module

    log = RepairLog()
    encoding = detect_encoding(path)
    if encoding.replace("-", "").lower() not in ("utf8", "utf8sig", "ascii"):
        log.add("encoding_fallback", detail=encoding)
        log.note("File encoding detected as {enc}; converted to UTF-8.", enc=encoding)

    # 1. sample logical lines
    sample = []  # (line_no, text, broken)
    with open_text(path, encoding) as f:
        for ln, text, broken in logical_lines(f):
            if text.strip():
                sample.append((ln, text, broken))
            if len(sample) >= config.SNIFF_LINES:
                break
    if not sample:
        raise ValueError("File is empty or contains no readable text.")
    plan = make_plan(sample, log)
    delim, m, split, header = plan["delim"], plan["m"], plan["split"], plan["header"]
    has_header, hdr_idx, header_ln = plan["has_header"], plan["hdr_idx"], plan["header_ln"]
    rep = plan_repairer(plan, log)

    # 3. choose the parse strategy
    out = work_dir / f"{name}.parquet"
    probe = RepairLog()
    rep.log = probe
    for ln, text, broken in sample[hdr_idx + 1 if has_header else hdr_idx:]:
        f_ = split(text, delim)
        if broken or rep.is_header_repeat(f_):
            probe.add("x")
        else:
            rep.fix(f_, ln, text)
    rep.log = log
    sample_dirty = sum(probe.counts.values()) > 0
    size = path.stat().st_size
    native_ok = (delim in CANDIDATES + EXTRA and m > 1
                 and encoding.replace("-", "").lower() in ("utf8", "utf8sig", "ascii", "latin1", "iso88591"))
    source = {"format": "delimited", "encoding": encoding, "delimiter": delim}

    # Large files (or clean ones) go through DuckDB's parallel reader; only the
    # lines it rejects are repaired in Python. Small dirty files get the full
    # Python repair pass, which additionally realigns sliding rows.
    if native_ok and (not sample_dirty or size > config.NATIVE_PARSE_MIN_BYTES):
        progress(0.1, "Parsing with native DuckDB reader")
        res = _duckdb_native(path, out, delim, header, header_ln, encoding, max_rows, progress, plan["esc"])
        if res is not None:
            n, rejects = res
            fixed = work_dir / f"{name}_repaired.parquet"
            sink = ParquetSink(fixed, header)
            for ln, text in rejects:
                for row in rep.fix(split(text, delim), ln, text):
                    sink.write(row)
            if rejects:
                log.note("{n:,} malformed lines rejected by the native reader were repaired and kept.", n=len(rejects))
            progress(0.45, "Checking parsed rows for misaligned values")
            out, n_kept, n_suspect = _recheck_suspects(out, header, plan["classes"], rep, sink, log, n)
            n = n_kept + sink.close()
            log.note("Parsed with the native DuckDB CSV reader (parallel, out-of-core).")
            return [Table(name=name, path=out, extra_paths=[fixed], columns=header, n_rows=n,
                          source=source, repairs=log)]
        log.note("Native reader could not parse the file: switched to the streaming repair parser.")

    # 4. repairing streaming parse
    progress(0.1, "Parsing with anomaly repair")
    total = size or 1
    sink = ParquetSink(out, header)
    seen_bytes = 0
    with open_text(path, encoding) as f:
        for ln, text, broken in logical_lines(f, esc=plan["esc"]):
            seen_bytes += len(text) + 1
            if ln <= header_ln:
                continue
            if not text.strip():
                log.counts["blank_line"] += 1
                continue
            if broken:
                fields = text.replace(QUOTE, "").split(delim) if delim not in ("WS", "WS2") else split(text, delim)
                log.add("unbalanced_quote", ln, text)
            else:
                fields = split(text, delim)
            if not any(x.strip() for x in fields):
                log.counts["empty_row"] += 1
                continue
            if m > 1 and rep.is_header_repeat(fields):
                log.add("repeated_header", ln, text)
                continue
            for row in (rep.fix(fields, ln, text) if m > 1 else [fields]):
                sink.write(row)
            if ln % 200_000 == 0:
                progress(0.1 + 0.4 * min(1.0, seen_bytes / total), f"Parsed {ln:,} lines")
            if max_rows and sink.rows + sink._n >= max_rows:
                log.note("Row limit reached: only the first {n:,} rows were profiled.", n=max_rows)
                break
    n = sink.close()
    return [Table(name=name, path=out, columns=header, n_rows=n, source=source, repairs=log)]


# SQL (RE2) twins of repair.NUM_RE / DATE_RE, used to spot misaligned values.
_SQL_NUM = r"[+-]?[$€£¥]?\s?\(?\d[\d,. '']*\)?\s?%?"
_SQL_DATE = (r"^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2,4})"
             r"([ T]\d{1,2}:\d{2}(:\d{2})?.*)?$")
MAX_SUSPECT_SHARE = 0.05


def _recheck_suspects(out: Path, header, classes, rep: RowRepairer, sink, log: RepairLog, n: int):
    """The native reader silently accepts some broken lines (e.g. an unquoted
    delimiter combined with an empty last field looks like a trailing delimiter,
    or a row shifted by one column). Find rows whose values contradict their
    column's learned type, re-repair just those in Python and rewrite the
    Parquet without them. Returns (path, rows kept, suspects)."""
    from ..duck import connect, ident

    conds = []
    for h, cls in zip(header, classes):
        x, v = ident(h), f"trim({ident(h)})"
        filled = f"({x} IS NOT NULL AND {v} <> '')"
        if cls == "I":
            conds.append(f"({filled} AND NOT regexp_full_match({v}, '\\d+'))")
        elif cls == "N":
            conds.append(f"({filled} AND NOT regexp_full_match({v}, '{_SQL_NUM}'))")
        elif cls == "D":
            conds.append(f"({filled} AND NOT regexp_matches({v}, '{_SQL_DATE}'))")
    if not conds:
        return out, n, 0
    cond = " OR ".join(conds)
    src = f"read_parquet('{out.as_posix()}')"
    con = connect(out.parent)
    try:
        k = con.execute(f"SELECT count(*) FROM {src} WHERE {cond}").fetchone()[0]
        if k == 0:
            return out, n, 0
        if k > MAX_SUSPECT_SHARE * n:
            log.note("{k:,} rows hold values that do not match their column type; too many to be structural "
                     "errors, so they were kept as parsed (see mixed-type warnings).", k=k)
            return out, n, 0
        cur = con.execute(f"SELECT * FROM {src} WHERE {cond}")
        changed = 0
        while True:
            chunk = cur.fetchmany(50_000)
            if not chunk:
                break
            for row in chunk:
                fields = ["" if v is None else v for v in row]
                rows, sub = _best_reading(rep, fields)
                if sub is not None:
                    changed += 1
                    for kind, c in sub.counts.items():
                        log.counts[kind] += c
                        exs = log.examples.setdefault(kind, [])
                        exs.extend(sub.examples.get(kind, [])[: 5 - len(exs)])
                for r in rows:
                    sink.write(r)
        clean = out.with_name(out.stem + "_clean.parquet")
        con.execute(f"COPY (SELECT * FROM {src} WHERE NOT ({cond})) TO '{clean.as_posix()}' "
                    f"(FORMAT parquet, COMPRESSION zstd)")
        out.unlink(missing_ok=True)
        log.note("{k:,} rows had values contradicting their column type; {c:,} of them were realigned.", k=k, c=changed)
        return clean, n - k, k
    finally:
        con.close()


def _best_reading(rep: RowRepairer, fields: list[str]):
    """Repair a natively parsed row both as-is and assuming the reader dropped
    an empty trailing field; keep the reading whose values fit best.
    Returns (rows, RepairLog or None when the row was left unchanged)."""
    real = rep.log
    base_score = rep.score(fields)
    best = (base_score, [fields], None)
    for cand in (fields, fields + [""]):
        tmp = RepairLog()
        rep.log = tmp
        rows = rep.fix(list(cand), None, None)
        sc = sum(rep.score(r) for r in rows) / len(rows)
        if tmp.counts and sc < best[0] - 0.5:
            best = (sc, rows, tmp)
    rep.log = real
    return best[1], best[2]


def _duckdb_native(path: Path, out: Path, delim: str, header: list[str], header_ln: int, encoding: str,
                   max_rows: int, progress, esc: str | None = None):
    """Parse with DuckDB, storing malformed lines in a rejects table.
    Returns (rows_written, [(line_no, raw_line), ...]) or None on failure."""
    from ..duck import connect, ident, run_with_progress

    enc = "latin-1" if encoding.replace("-", "").lower() in ("latin1", "iso88591") else "utf-8"
    con = connect(out.parent)
    try:
        cols = "{" + ", ".join(f"{_sql_str(h)}: 'VARCHAR'" for h in header) + "}"
        src = path.as_posix().replace("'", "''")
        # drop header lines repeated inside the data
        not_header = " AND ".join(f'{ident(h)} IS NOT DISTINCT FROM {_sql_str(h)}' for h in header[:3])
        limit = f" LIMIT {int(max_rows)}" if max_rows else ""
        sql = (f"COPY (SELECT * FROM read_csv('{src}', delim={_sql_str(delim)}, quote='\"', escape={_sql_str(esc or QUOTE)}, "
               f"header=false, skip={header_ln}, columns={cols}, strict_mode=true, null_padding=false, "
               f"store_rejects=true, rejects_table='rej', rejects_scan='rej_scan', rejects_limit=0, "
               f"encoding='{enc}', auto_detect=false, max_line_size=10000000) "
               f"WHERE NOT ({not_header}){limit}) TO '{out.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
        run_with_progress(con, sql, lambda p: progress(0.1 + 0.4 * p, f"Native parse {p:.0%}"))
        n = con.execute(f"SELECT count(*) FROM read_parquet('{out.as_posix()}')").fetchone()[0]
        rejects = con.execute("SELECT line, any_value(csv_line) FROM rej GROUP BY line ORDER BY line").fetchall()
        return n, [(int(ln), (txt or "").lstrip("\r\n")) for ln, txt in rejects
                   if txt and txt.strip()][: max(0, max_rows - n) if max_rows else None]
    except Exception:   # duckdb.Error: fall back to the streaming repair parser
        return None
    finally:
        con.close()


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
