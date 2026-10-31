"""Already-structured formats: Parquet, JSON/NDJSON, Arrow/Feather, ORC, SQLite, XML."""
from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import duckdb

from .. import config
from ..models import RepairLog, Table, clean_header
from .sink import ParquetSink


def _con():
    from ..duck import connect
    return connect()


def _table_from_parquet(path: Path, name: str, fmt: str, log: RepairLog | None = None) -> Table:
    con = _con()
    try:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')").fetchall()]
        n = con.execute(f"SELECT count(*) FROM read_parquet('{path.as_posix()}')").fetchone()[0]
    finally:
        con.close()
    return Table(name=name, path=path, columns=cols, n_rows=n, source={"format": fmt}, repairs=log or RepairLog())


def ingest_parquet(path: Path, work_dir: Path, name: str, progress=lambda p, m: None):
    return [_table_from_parquet(path, name, "parquet")]


def ingest_json(path: Path, work_dir: Path, name: str, progress=lambda p, m: None, max_rows: int = 0):
    from ..duck import run_with_progress

    out = work_dir / f"{name}.parquet"
    limit = f" LIMIT {int(max_rows)}" if max_rows else ""
    con = _con()
    try:
        run_with_progress(con, f"COPY (SELECT * FROM read_json_auto('{path.as_posix()}', format='auto', "
                               f"maximum_object_size=104857600, sample_size=20000){limit}) "
                               f"TO '{out.as_posix()}' (FORMAT parquet, COMPRESSION zstd)",
                          lambda p: progress(0.1 + 0.4 * p, f"Parsing JSON {p:.0%}"))
    finally:
        con.close()
    log = RepairLog()
    log.note("Nested objects/arrays are profiled as JSON text.")
    return [_table_from_parquet(out, name, "json", log)]


def ingest_arrow(path: Path, work_dir: Path, name: str, progress=lambda p, m: None):
    import pyarrow.parquet as pq

    out = work_dir / f"{name}.parquet"
    if path.suffix.lower() == ".orc":
        import pyarrow.orc as orc
        f = orc.ORCFile(str(path))
        with pq.ParquetWriter(str(out), f.schema) as w:
            for i in range(f.nstripes):
                w.write_table(f.read_stripe(i) if hasattr(f.read_stripe(i), "schema") else f.read_stripe(i))
    else:
        import pyarrow.ipc as ipc
        import pyarrow as pa
        try:
            reader = ipc.open_file(pa.memory_map(str(path)))
            batches = (reader.get_batch(i) for i in range(reader.num_record_batches))
        except pa.ArrowInvalid:
            reader = ipc.open_stream(pa.memory_map(str(path)))
            batches = iter(reader)
        with pq.ParquetWriter(str(out), reader.schema) as w:
            for b in batches:
                w.write_batch(b)
    return [_table_from_parquet(out, name, path.suffix.lstrip("."))]


def ingest_sqlite(path: Path, work_dir: Path, name: str, progress=lambda p, m: None):
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    tables = []
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for ti, tname in enumerate(names):
            progress(0.05 + 0.4 * ti / max(1, len(names)), f"Reading table {tname}")
            cur = con.execute(f'SELECT * FROM "{tname}"')
            cols = clean_header([d[0] for d in cur.description])
            sink = ParquetSink(work_dir / f"{name}_{ti}.parquet", cols)
            while True:
                chunk = cur.fetchmany(50_000)
                if not chunk:
                    break
                for row in chunk:
                    sink.write([None if v is None else (v.hex() if isinstance(v, bytes) else str(v)) for v in row])
            n = sink.close()
            tables.append(Table(name=tname, path=sink.path, columns=cols, n_rows=n, source={"format": "sqlite"}))
    finally:
        con.close()
    return tables


def ingest_xml(path: Path, work_dir: Path, name: str, progress=lambda p, m: None):
    """Records = the most frequent element tag at depth 1 (or 2); fields =
    attributes + child elements' text. Streamed with iterparse."""
    depth_tags = Counter()
    depth = 0
    for ev, el in ET.iterparse(str(path), events=("start", "end")):
        if ev == "start":
            depth += 1
            if depth in (2, 3):
                depth_tags[(depth, el.tag)] += 1
        else:
            depth -= 1
            if sum(depth_tags.values()) > 20000:
                break
    if not depth_tags:
        raise ValueError("XML has no repeating records.")
    (rec_depth, rec_tag), _ = max(depth_tags.items(), key=lambda kv: (kv[1], -kv[0][0]))

    def records():
        d = 0
        for ev, el in ET.iterparse(str(path), events=("start", "end")):
            if ev == "start":
                d += 1
                continue
            if d == rec_depth and el.tag == rec_tag:
                rec = {f"@{k}": v for k, v in el.attrib.items()}
                for ch in el:
                    rec[ch.tag] = (ch.text or "").strip() if len(ch) == 0 else ET.tostring(ch, encoding="unicode")[:500]
                if not rec and el.text:
                    rec["value"] = el.text.strip()
                yield rec
                el.clear()
            d -= 1

    keys: dict = {}
    for i, rec in enumerate(records()):
        for k in rec:
            keys.setdefault(k, None)
        if i > 5000:
            break
    cols = list(keys)
    header = clean_header(cols)
    sink = ParquetSink(work_dir / f"{name}.parquet", header)
    for rec in records():
        sink.write([rec.get(k) for k in cols])
    n = sink.close()
    log = RepairLog()
    log.note("XML records taken from <{tag}> elements.", tag=rec_tag)
    return [Table(name=name, path=sink.path, columns=header, n_rows=n, source={"format": "xml"}, repairs=log)]
