"""DuckDB connection helpers: bounded memory, spill-to-disk, progress polling."""
from __future__ import annotations

import threading
from pathlib import Path

import duckdb

from . import cancel, config


def connect(spill_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    # Air-gapped servers: never try to download extensions (the ones used here are built in).
    con = duckdb.connect(config={"autoinstall_known_extensions": False})
    cancel.on_cancel(con.interrupt)   # a stop request aborts the running query
    con.execute(f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads={config.DUCKDB_THREADS}")
    con.execute("SET preserve_insertion_order=false")
    tmp = (spill_dir or config.WORK_DIR) / "duck_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp.as_posix()}'")
    con.execute("SET enable_progress_bar=true")
    con.execute("SET enable_progress_bar_print=false")
    return con


def run_with_progress(con, sql: str, cb, interval: float = 1.0):
    """Execute `sql`, calling cb(fraction) while it runs."""
    err: list = []

    def work():
        try:
            con.execute(sql)
        except Exception as e:  # re-raised in caller thread
            err.append(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    while t.is_alive():
        t.join(interval)
        if cancel.cancelled():
            con.interrupt()
            t.join()
            cancel.check()
        try:
            p = con.query_progress()
            if p is not None and p >= 0:
                cb(min(1.0, p / 100))
        except Exception:
            pass
    cancel.check()
    if err:
        raise err[0]


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def parquet_src(paths) -> str:
    return "read_parquet([" + ", ".join(lit(Path(p).as_posix()) for p in paths) + "], union_by_name=true)"
