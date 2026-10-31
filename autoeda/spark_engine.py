"""Optional Spark engine for cluster-scale inputs (HDFS / S3 / shared storage).

* Ingest: delimited text is sniffed on a sample with the same logic as the
  local engine, then every line is parsed + repaired on the executors
  (mapPartitions with RowRepairer) and materialised as Parquet in
  AUTOEDA_SPARK_WORK_DIR. Parquet / ORC / JSON are read natively.
* Profiling: type inference, patterns and merged-column checks run in DuckDB
  on a sample collected to the driver; all full-data statistics are Spark SQL
  jobs. The result has exactly the same structure as the local engine's.

Requires `pyspark` (and Java) on the driver; enable with engine=spark.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import config
from .models import RepairLog, Table
from .profiling import (FALSE_TOKENS, INVISIBLE_SQL, MOJIBAKE_SQL, NULL_TOKENS, QUANTS, TRUE_TOKENS, TableProfiler,
                        _flag, _fmt, _pct, _py)

SPARK_TEXT_EXT = {".csv", ".tsv", ".txt", ".dat", ".psv", ".tab", ".gz", ".bz2", ".prn"}
SPARK_FORMATS = {".parquet": "parquet", ".orc": "orc", ".json": "json", ".jsonl": "json", ".ndjson": "json"}

_SPARK = None


def available() -> bool:
    try:
        import pyspark  # noqa: F401
        return True
    except ImportError:
        return False


def get_spark():
    """Create (once) the SparkSession. Master and extra conf come from env:
    AUTOEDA_SPARK_MASTER (e.g. spark://host:7077, yarn, local[*]) and
    AUTOEDA_SPARK_CONF ("key=value;key=value")."""
    global _SPARK
    if _SPARK is not None:
        return _SPARK
    from pyspark.sql import SparkSession

    master = os.getenv("AUTOEDA_SPARK_MASTER", "local[*]")
    if master.startswith("local"):
        # single-machine mode: bind to loopback so no hostname resolution / network is needed
        os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
        os.environ.setdefault("SPARK_LOCAL_HOSTNAME", "localhost")
    b = SparkSession.builder.appName("autoeda").master(master)
    b = (b.config("spark.sql.ansi.enabled", "false")
          .config("spark.sql.legacy.timeParserPolicy", "CORRECTED")
          .config("spark.sql.adaptive.enabled", "true"))
    for kv in filter(None, os.getenv("AUTOEDA_SPARK_CONF", "").split(";")):
        k, _, v = kv.partition("=")
        b = b.config(k.strip(), v.strip())
    spark = b.getOrCreate()
    spark.sparkContext.addPyFile(_package_zip())
    _SPARK = spark
    return spark


def _package_zip() -> str:
    """Ship the autoeda package to executors (RowRepairer runs there)."""
    pkg = Path(__file__).resolve().parent
    out = Path(tempfile.gettempdir()) / "autoeda_pkg.zip"
    with zipfile.ZipFile(out, "w") as z:
        for f in pkg.rglob("*.py"):
            z.write(f, f"autoeda/{f.relative_to(pkg).as_posix()}")
    return str(out)


def to_uri(path: str) -> str:
    return path if "://" in path else Path(path).resolve().as_uri()


# --- ingest ------------------------------------------------------------------
def handles(path: str) -> bool:
    p = path.lower().rstrip("/")
    ext = os.path.splitext(p)[1]
    return ext in SPARK_TEXT_EXT or ext in SPARK_FORMATS or ext == ""


def ingest(path: str, job_id: str, progress=lambda p, m: None, max_rows: int = 0) -> list[Table]:
    spark = get_spark()
    uri = to_uri(path)
    ext = os.path.splitext(path.lower().rstrip("/"))[1]
    name = Path(path.rstrip("/")).stem or "data"
    if ext in SPARK_FORMATS or ext == "":
        fmt = SPARK_FORMATS.get(ext, "parquet")
        reader = spark.read.option("multiLine", "false") if fmt == "json" else spark.read
        df = getattr(reader, fmt)(uri)
        if max_rows:
            df = df.limit(max_rows)
        log = RepairLog()
        log.note("Read natively by Spark as {fmt}.", fmt=fmt)
        return [Table(name=name, path=Path(path), columns=df.columns, n_rows=-1,
                      source={"format": fmt, "engine": "spark", "df": df}, repairs=log)]
    return [_ingest_text(spark, uri, name, job_id, progress, max_rows)]


def _ingest_text(spark, uri, name, job_id, progress, max_rows) -> Table:
    from pyspark.sql.types import StringType, StructField, StructType

    from .ingest.delimited import make_plan

    log = RepairLog()
    progress(0.05, "Sniffing structure on a sample")
    lines = [r.value for r in spark.read.text(uri).limit(config.SNIFF_LINES * 2).collect()]
    sample = [(i + 1, t, t.count('"') % 2 == 1) for i, t in enumerate(lines) if t.strip()][: config.SNIFF_LINES]
    if not sample:
        raise ValueError("File is empty.")
    plan = make_plan(sample, log)
    log.note("Spark engine parses line by line: quoted values spanning several lines are not re-joined.")
    plan_ship = {k: v for k, v in plan.items() if k != "split"}
    acc = spark.sparkContext.accumulator({}, _DictAcc())

    def parse_partition(it):
        from functools import partial

        from autoeda.ingest.delimited import QUOTE, plan_repairer, split_line, split_single
        from autoeda.models import RepairLog as RL

        plog = RL()
        rep = plan_repairer(plan_ship, plog)
        m, d = plan_ship["m"], plan_ship["delim"]
        split = split_single if m == 1 else partial(split_line, esc=plan_ship.get("esc"))
        skip = set(plan_ship["skip_texts"])
        header_text = plan_ship["skip_texts"][-1] if plan_ship["has_header"] else None
        for text in it:
            if not text.strip():
                continue
            if text in skip:
                if text == header_text:   # header line (first copy is subtracted on the driver)
                    plog.counts["repeated_header"] += 1
                continue
            if text.count(QUOTE) % 2 == 1 and d not in ("WS", "WS2"):
                fields = text.replace(QUOTE, "").split(d)
                plog.add("unbalanced_quote", None, text)
            else:
                fields = split(text, d)
            if not any(x.strip() for x in fields):
                plog.counts["empty_row"] += 1
                continue
            if m > 1 and rep.is_header_repeat(fields):
                plog.add("repeated_header", None, text)
                continue
            for row in (rep.fix(fields, None, text) if m > 1 else [fields]):
                yield [None if v == "" else v for v in row]
        acc.add({k: [c, plog.examples.get(k, [])] for k, c in plog.counts.items()})

    schema = StructType([StructField(h, StringType(), True) for h in plan["header"]])
    rdd = spark.read.text(uri).rdd.map(lambda r: r.value).mapPartitions(parse_partition)
    df = spark.createDataFrame(rdd, schema)
    if max_rows:
        df = df.limit(max_rows)
    out = f"{config.SPARK_WORK_DIR.rstrip('/')}/{job_id}/{name}.parquet"
    progress(0.15, "Parsing + repairing on the cluster")
    df.write.mode("overwrite").parquet(out)
    for kind, (cnt, exs) in acc.value.items():
        if kind == "repeated_header":
            cnt -= 1    # the real header line itself
            if cnt <= 0:
                continue
        log.counts[kind] += cnt
        log.examples.setdefault(kind, []).extend(exs[: 5 - len(log.examples.get(kind, []))])
    df = spark.read.parquet(out)
    return Table(name=name, path=Path(out), columns=plan["header"], n_rows=-1,
                 source={"format": "delimited", "engine": "spark", "delimiter": plan["delim"], "df": df},
                 repairs=log)


try:
    from pyspark.accumulators import AccumulatorParam

    class _DictAcc(AccumulatorParam):
        def zero(self, v):
            return {}

        def addInPlace(self, a, b):
            for k, (c, ex) in b.items():
                ca, exa = a.get(k, [0, []])
                a[k] = [ca + c, (exa + ex)[:5]]
            return a
except ImportError:  # pyspark not installed: engine unavailable
    _DictAcc = None


# --- profiling ---------------------------------------------------------------
def bt(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def slit(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _sum_if(cond: str) -> str:
    return f"sum(CASE WHEN {cond} THEN 1 ELSE 0 END)"


STRFTIME_TO_JAVA = [("%Y", "yyyy"), ("%y", "yy"), ("%m", "MM"), ("%d", "dd"), ("%H", "HH"), ("%I", "hh"),
                    ("%M", "mm"), ("%S", "ss"), ("%p", "a"), ("%B", "MMMM"), ("%b", "MMM")]


def _java_fmt(fmt: str) -> str:
    for a, b in STRFTIME_TO_JAVA:
        fmt = fmt.replace(a, b)
    return fmt


class SparkProfiler(TableProfiler):
    engine = "spark"

    def __init__(self, table: Table, progress=lambda p, m: None):
        super().__init__(table, progress)   # DuckDB connection for the local sample
        self.spark = get_spark()
        self.df = table.source.pop("df")

    def sq(self, sql: str):
        return self.spark.sql(sql).collect()

    def sq1(self, sql: str):
        return self.sq(sql)[0]

    def _setup(self):
        from pyspark.sql import functions as F
        import pyarrow as pa
        import pyarrow.parquet as pq

        df = self.df
        cols, sel = [], []
        for f in df.schema.fields:
            if f.name in self.t.drop_columns:
                continue
            typ = f.dataType.simpleString().upper()
            k = ("num" if typ.split("(")[0] in ("TINYINT", "SMALLINT", "INT", "BIGINT", "FLOAT", "DOUBLE", "DECIMAL")
                 else "bool" if typ == "BOOLEAN" else "date" if typ in ("DATE", "TIMESTAMP", "TIMESTAMP_NTZ")
                 else "str" if typ == "STRING" else "other")
            if k == "other":
                sel.append(F.to_json(F.col(bt(f.name))).alias(f.name) if typ.startswith(("STRUCT", "ARRAY", "MAP"))
                           else F.col(bt(f.name)).cast("string").alias(f.name))
                k = "str"
            else:
                sel.append(F.col(bt(f.name)))
            cols.append({"name": f.name, "physical_type": typ, "kind": k})
        b = df.select(*sel)
        b.createOrReplaceTempView("b")
        self.progress(0.5, "Counting rows on the cluster")
        self.n = b.count()
        self.t.n_rows = self.n
        self.huge = True                       # always use approximate algorithms on Spark
        sample_n = max(1000, min(config.SAMPLE_ROWS, config.SAMPLE_CELLS // max(1, len(cols))))
        frac = min(1.0, sample_n * 1.2 / max(1, self.n))
        self.progress(0.52, f"Collecting a {sample_n:,}-row sample to the driver")
        rows = b.sample(False, frac, seed=42).limit(sample_n).collect() if frac < 1 else b.collect()
        local = Path(tempfile.mkdtemp(dir=config.WORK_DIR)) / "sample.parquet"
        data = {c["name"]: [r[i] for r in rows] for i, c in enumerate(cols)}
        arrays = []
        for c in cols:
            try:
                arrays.append(pa.array(data[c["name"]]))
            except (pa.ArrowInvalid, pa.ArrowTypeError):
                arrays.append(pa.array([None if v is None else str(v) for v in data[c["name"]]], pa.string()))
        pq.write_table(pa.Table.from_arrays(arrays, names=[c["name"] for c in cols]), local)
        self.con.execute(f"CREATE TEMP TABLE s AS SELECT * FROM read_parquet('{local.as_posix()}')")
        self.sample_n = len(rows)
        self._local = local
        return cols

    # ---- Spark SQL dialect ------------------------------------------------
    def snull(self, c) -> str:
        x = bt(c["name"])
        if c["kind"] == "str":
            toks = ", ".join(slit(s) for s in NULL_TOKENS)
            return f"({x} IS NULL OR trim({x}) = '' OR lower(trim({x})) IN ({toks}))"
        if c["physical_type"] in ("DOUBLE", "FLOAT"):
            return f"({x} IS NULL OR isnan({x}))"
        return f"({x} IS NULL)"

    def sconv(self, d, c) -> str:
        x = bt(c["name"])
        v = f"trim({x})"
        t = ", ".join(slit(s) for s in TRUE_TOKENS)
        f = ", ".join(slit(s) for s in FALSE_TOKENS)
        neg = (f"(CASE WHEN {v} RLIKE {slit(r'^\s*[^0-9]*\(.*\)\s*[*†‡¹²³]?$')} OR {v} RLIKE {slit('^[^0-9]*-')} "
               f"OR {v} RLIKE {slit(r'[0-9]\s*-\s*$')} THEN -1 ELSE 1 END)")
        return {
            "num_native": f"CAST({x} AS DOUBLE)",
            "bool_native": x,
            "date_native": f"CAST({x} AS TIMESTAMP)",
            "bool_str": f"CASE WHEN lower({v}) IN ({t}) THEN true WHEN lower({v}) IN ({f}) THEN false END",
            "double": f"CAST({v} AS DOUBLE)",
            "num_us": f"({neg} * CAST(regexp_replace({v}, '[^0-9.]', '') AS DOUBLE))",
            "num_eu": f"({neg} * CAST(replace(regexp_replace({v}, '[^0-9,]', ''), ',', '.') AS DOUBLE))",
            "unit": f"CAST(replace(regexp_extract({v}, '^[-+]?[0-9]+([.,][0-9]+)?', 0), ',', '.') AS DOUBLE)",
            "ts": f"CAST({v} AS TIMESTAMP)",
            "ts_fmt": f"coalesce(CAST({v} AS TIMESTAMP), to_timestamp({v}, {slit(_java_fmt(d.get('fmt', '')))}))",
        }.get(d.get("conv"), x)

    # ---- full-data passes on Spark ---------------------------------------
    def _pass_basic(self, cols):
        parts = []
        for c in cols:
            x = bt(c["name"])
            parts += [_sum_if(self.snull(c)), _sum_if(f"{x} IS NULL"), f"approx_count_distinct({x}, 0.01)"]
            if c["kind"] == "str":
                parts += [f"min(length({x}))", f"max(length({x}))", f"avg(length({x}))",
                          _sum_if(f"{x} <> trim({x}) AND trim({x}) <> ''"),
                          _sum_if(f"{x} RLIKE {slit(INVISIBLE_SQL)}"),
                          _sum_if(f"instr({x}, char(10)) > 0 OR instr({x}, char(13)) > 0"),
                          _sum_if(f"{x} RLIKE {slit(MOJIBAKE_SQL)}")]
            else:
                parts += ["NULL", "NULL", "NULL", "0", "0", "0", "0"]
        row = self.sq1(f"SELECT {', '.join(parts)} FROM b")
        for j, c in enumerate(cols):
            v = row[j * 10:(j + 1) * 10]
            c.update(missing=v[0] or 0, hard_null=v[1] or 0, distinct_approx=v[2] or 0,
                     len_min=v[3], len_max=v[4], len_avg=_py(v[5]), whitespace=v[6] or 0,
                     invisible=v[7] or 0, multiline=v[8] or 0, mojibake=v[9] or 0)

    def _top(self, c, expr_sql=None, k=10, use_sample=False):
        if use_sample:
            return super()._top(c, expr_sql, k, True)
        x = bt(c["name"])
        rows = self.sq(f"SELECT CAST({x} AS STRING) v, count(*) n FROM b WHERE NOT {self.snull(c)} "
                       f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {k}")
        base = self.n - c["missing"]
        return [[r[0], r[1], _pct(r[1], base)] for r in rows]

    def _need_sample_top(self, c):
        return c["distinct_approx"] > 100_000

    def _invalid_examples(self, c, d):
        x = bt(c["name"])
        return [[r[0], r[1]] for r in self.sq(
            f"SELECT CAST({x} AS STRING), count(*) FROM b WHERE NOT {self.snull(c)} AND ({self.sconv(d, c)}) IS NULL "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT 5")]

    def _numeric(self, c, d, p):
        e, nl = self.sconv(d, c), self.snull(c)
        r = self.sq1(f"""SELECT count(x), min(x), max(x), avg(x), stddev_samp(x),
              percentile_approx(x, array({', '.join(map(str, QUANTS))}), 10000), skewness(x), kurtosis(x),
              {_sum_if('x = 0')}, {_sum_if('x < 0')}, {_sum_if('x <> floor(x)')},
              {_sum_if('NOT nl AND x IS NULL')}, sum(x)
            FROM (SELECT {e} AS x, {nl} AS nl FROM b) WHERE x IS NULL OR NOT (isnan(x) OR abs(x) = double('inf'))""")
        cnt, lo, hi, mean, sd, qs, skew, kurt, zeros, neg, frac, invalid, total = [_py(v) for v in r]
        if not cnt:
            p["inferred_type"] = "text"
            self._text(c, {"type": "text"}, p)
            return
        qs = [float(q) for q in qs] if qs else [None] * 7
        qd = dict(zip(["p1", "p5", "q1", "median", "q3", "p95", "p99"], qs))
        is_int = (frac or 0) == 0
        p["inferred_type"] = "integer" if is_int else "float"
        num = {"count": cnt, "min": lo, "max": hi, "mean": mean, "std": sd, "skew": skew, "kurtosis": kurt,
               "zeros": zeros, "negatives": neg, "sum": total, **qd,
               "cv": (sd / abs(mean)) if sd is not None and mean else None}
        iqr = qd["q3"] - qd["q1"]
        lof, hif = qd["q1"] - 1.5 * iqr, qd["q3"] + 1.5 * iqr
        # histogram + outliers in one job
        if lo == hi:
            w, nb = 1.0, 1
        elif is_int and hi - lo < 40:
            w, nb = 1.0, int(hi - lo) + 1
        else:
            nb = 24
            w = (hi - lo) / nb
        rows = self.sq(f"""SELECT CAST(least(floor((x - {lo}) / {w}), {nb - 1}) AS BIGINT) b, count(*) n,
                  {_sum_if(f'x < {lof} OR x > {hif}')} o
                FROM (SELECT {e} AS x FROM b) WHERE x IS NOT NULL AND NOT isnan(x) GROUP BY 1""")
        hist = {r[0]: r[1] for r in rows}
        o = sum(r[2] for r in rows)
        bins = []
        for i in range(nb):
            a = lo + i * w
            bins.append([_fmt(a) if w == 1.0 and is_int else f"{_fmt(a)} – {_fmt(a + w)}", hist.get(i, 0)])
        p["histogram"] = {"kind": "numeric", "bins": bins}
        num.update(outliers=o, outlier_low=lof, outlier_high=hif, outlier_pct=_pct(o, cnt))
        p["numeric"] = num
        distinct = c["distinct_approx"]
        if is_int and distinct <= 50 or distinct <= 20:
            p["top_values"] = self._top(c)
        if is_int and distinct >= 0.97 * cnt and cnt >= 20:   # HLL error margin
            p["semantic"] = "identifier"
        elif is_int and distinct == 2 and lo == 0 and hi == 1:
            p["semantic"] = "binary flag"
        elif is_int and distinct <= 15:
            p["semantic"] = "discrete / code"
        flags = p["flags"]
        if invalid:
            flags.append(_flag("warn", "mixed_type",
                               "{n:,} non-numeric values ({pct:.1f}%) in a numeric column.",
                               n=invalid, pct=_pct(invalid, cnt + invalid),
                               examples=self._invalid_examples(c, d)))
        if o and p["semantic"] != "identifier":
            flags.append(_flag("info", "outliers", "{n:,} unusually small or large values ({pct:.2f}%) outside the "
                                                   "typical range [{lo}, {hi}].", n=o, pct=_pct(o, cnt),
                               lo=float(lof), hi=float(hif)))
        if skew is not None and abs(skew) >= 2 and p["semantic"] != "identifier":
            flags.append(_flag("info", "skew", ("Most values are small with a few very large ones (right-skewed, skew {s:.2f})."
                                          if skew > 0 else
                                          "Most values are large with a few very small ones (left-skewed, skew {s:.2f})."),
                               s=skew))

    def _datetime(self, c, d, p):
        e, nl = self.sconv(d, c), self.snull(c)
        r = self.sq1(f"""SELECT count(x), min(x), max(x), {_sum_if('x > current_timestamp()')},
              {_sum_if("x < TIMESTAMP '1900-01-01'")}, {_sum_if("date_format(x, 'HH:mm:ss') <> '00:00:00'")},
              {_sum_if('NOT nl AND x IS NULL')}, percentile_approx(unix_timestamp(x), 0.5)
            FROM (SELECT CAST({e} AS TIMESTAMP) AS x, {nl} AS nl FROM b)""")
        cnt, lo, hi, future, ancient, with_time, invalid, med = r
        if not cnt:
            p["inferred_type"] = "text"
            self._text(c, {"type": "text"}, p)
            return
        import datetime as dt
        span = (hi - lo).days
        p["inferred_type"] = "datetime" if with_time else "date"
        p["datetime"] = {"count": cnt, "min": _py(lo), "max": _py(hi), "span_days": span, "future": future,
                         "before_1900": ancient, "has_time": bool(with_time),
                         "median": _py(dt.datetime.utcfromtimestamp(med)) if med is not None else None}
        grain = "year" if span > 3 * 365 else "month" if span > 90 else "day"
        fmt = {"year": "yyyy", "month": "yyyy-MM", "day": "yyyy-MM-dd"}[grain]
        rows = self.sq(f"SELECT date_format(x, '{fmt}') k, count(*) FROM (SELECT CAST({e} AS TIMESTAMP) AS x FROM b) "
                       f"WHERE x IS NOT NULL GROUP BY 1 ORDER BY 1")
        rows = [[r[0], r[1]] for r in rows]
        if len(rows) > 120:
            step = -(-len(rows) // 120)
            rows = [[rows[i][0], sum(n for _, n in rows[i:i + step])] for i in range(0, len(rows), step)]
        p["histogram"] = {"kind": "date", "grain": grain, "bins": rows}
        wd = self.sq(f"SELECT dayofweek(x), count(*) FROM (SELECT CAST({e} AS TIMESTAMP) AS x FROM b) "
                     f"WHERE x IS NOT NULL GROUP BY 1 ORDER BY 1")
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        p["datetime"]["weekday"] = [[names[int(k) - 1], n] for k, n in wd]
        flags = p["flags"]
        if invalid:
            flags.append(_flag("warn", "mixed_type", "{n:,} values could not be read as dates.", n=invalid,
                               examples=self._invalid_examples(c, d)))
        if future:
            flags.append(_flag("info", "future_dates", "{n:,} dates are in the future.", n=future))
        if ancient:
            flags.append(_flag("warn", "old_dates", "{n:,} dates before 1900 (possible placeholder like 1900-01-01).", n=ancient))

    def _boolean(self, c, d, p):
        e = self.sconv(d, c)
        r = self.sq1(f"SELECT {_sum_if('x')}, {_sum_if('NOT x')} FROM (SELECT {e} AS x FROM b)")
        p["boolean"] = {"true": r[0] or 0, "false": r[1] or 0}
        p["top_values"] = self._top(c, None, 6)
        p["histogram"] = {"kind": "category", "bins": [["true", r[0] or 0], ["false", r[1] or 0]]}

    def _duplicates(self, cols):
        if not cols:
            return None
        h = "xxhash64(" + ", ".join(bt(c["name"]) for c in cols[:200]) + ")"
        d = self.sq1(f"SELECT approx_count_distinct({h}, 0.01) FROM b")[0]
        return {"count": max(0, self.n - d), "pct": _pct(max(0, self.n - d), self.n), "approximate": True}

    def run(self):
        try:
            return super().run()
        finally:
            shutil.rmtree(getattr(self, "_local", Path("_none_")).parent, ignore_errors=True)


def profile_table(table: Table, progress=lambda p, m: None) -> dict:
    return SparkProfiler(table, progress).run()
