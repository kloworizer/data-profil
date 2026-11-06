"""Column-level descriptive profiling on DuckDB.

Everything runs as SQL over Parquet so memory stays bounded and DuckDB can
spill to disk. Type inference runs on a reservoir sample; statistics run on
the full data (approximate algorithms above config.HUGE_ROWS rows).
"""
from __future__ import annotations

import datetime as dt
import decimal
import contextlib
import math
import re

from . import config
from .duck import connect, ident, lit, parquet_src
from .models import Table, msg

NULL_TOKENS = ["na", "n/a", "n.a.", "n.a", "null", "none", "nan", "-", "--", "---", "?", "#n/a", "nil", "missing",
               "undefined", "#value!", "#ref!", "#div/0!", "#name?", "#num!", "#null!", "#getting_data", "(blank)",
               "unknown", "tidak ada", "kosong", "tbd", "#"]
# Formatted numbers: optional sign / "(" / currency symbol or code (Rp, USD), digit groups with
# . , ' space NBSP, optional %, trailing minus or ")", optional footnote mark.
FMT_NUM_RE = (r"[(\-+]?\s*([$€£¥₹]|(?i:rp|idr|usd|us\$|eur|sgd|s\$|rm|rs|inr|jpy|gbp|aud)\.?)?\s*[(\-]?"
              r"\d[\d.,'\s\x{00A0}]*\s*%?\s*[)\-]?\s*[*†‡¹²³]?")
EU_STYLE_RE = r"\d{1,3}(\.\d{3})+(,\d+)?([^\d]*)$|\d,\d{1,2}([^\d]*)$"
US_STYLE_RE = r"\d{1,3}(,\d{3})+(\.\d+)?([^\d]*)$|\d\.\d{1,2}([^\d]*)$"
NEG_SIGN = (r"(CASE WHEN regexp_matches({v}, '^\s*[^0-9]*\(.*\)\s*[*†‡¹²³]?$') OR regexp_matches({v}, '^[^0-9]*-') "
            r"OR regexp_matches({v}, '\d\s*-\s*$') THEN -1 ELSE 1 END)")
INVISIBLE_SQL = r"[\x{00A0}\x{200B}\x{200C}\x{200D}\x{FEFF}\x{00AD}\x{0001}-\x{0008}\x{000B}\x{000C}\x{000E}-\x{001F}]"
MOJIBAKE_SQL = r"Ã[\x{0080}-\x{00BF}]|â€|Â[\x{00A0}-\x{00BF}]|\x{FFFD}"
UNIT_HEADER_RE = re.compile(r"\(([^)]{1,20})\)|\[([^\]]{1,20})\]|\b(in\s+(thousands|millions|billions)|"
                            r"dalam\s+(ribuan|jutaan|miliar)|ribu|juta|miliar|usd|idr|eur|rp|kg|km|pcs|%)\b", re.I)
SERIAL_DATE_NAME_RE = re.compile(r"date|tgl|tanggal|time|periode?|period|day|hari|dt\b|_at$", re.I)
PERIOD_NAME_RE = re.compile(r"^(19|20)\d{2}([-/ ]?(0?[1-9]|1[0-2]|q[1-4]|h[12]))?$|^q[1-4]([ -]?(19|20)\d{2})?$|"
                            r"^(jan|feb|mar|apr|may|mei|jun|jul|aug|agu|agt|sep|oct|okt|nov|dec|des)[a-z]*([ -]?\d{2,4})?$", re.I)
TRUE_TOKENS = ["true", "yes", "y", "t"]
FALSE_TOKENS = ["false", "no", "n", "f"]
DATE_FORMATS = ["%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%y", "%m/%d/%y",
                "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p",
                "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%b-%y", "%Y%m%d"]
QUANTS = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
SEMANTIC_RE = {
    "email": r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}",
    "url": r"(https?://|www\.)\S+",
    "uuid": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
    "ip_address": r"\d{1,3}(\.\d{1,3}){3}",
    "phone": r"\+?[\d][\d\s().-]{6,18}\d",
    "json": r"[\[{].*[\]}]",
}
MERGE_SEPS = [" - ", " | ", "|", ";", ", ", " / ", ":", "_", " "]
NUM_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
             "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL")


def _kind(typ: str) -> str:
    t = typ.upper()
    if t.startswith(NUM_TYPES):
        return "num"
    if t == "BOOLEAN":
        return "bool"
    if t == "DATE" or t.startswith("TIMESTAMP"):
        return "date"
    if t == "VARCHAR":
        return "str"
    return "other"


def _py(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_py(x) for x in v]
    return v


def _pct(a, b):
    return 100.0 * a / b if b else 0.0


class TableProfiler:
    def __init__(self, table: Table, progress=lambda p, m: None):
        self.t = table
        self.progress = progress
        self.con = connect()

    def q(self, sql):
        return self.con.execute(sql).fetchall()

    def q1(self, sql):
        return self.con.execute(sql).fetchone()

    # ------------------------------------------------------------------
    def run(self) -> dict:
        t = self.t
        cols = self._setup()
        self.progress(0.55, "Scanning completeness and cardinality")
        self._pass_basic(cols)
        profiles = []
        for i, c in enumerate(cols):
            self.progress(0.58 + 0.3 * i / max(1, len(cols)), f"Profiling column {i + 1}/{len(cols)}: {c['name']}")
            profiles.append(self._profile_column(i, c))
        self.progress(0.88, "Dataset-level checks")
        result = {
            "name": t.name, "source": t.source, "n_rows": self.n, "n_cols": len(cols),
            "approximate": self.huge, "sample_rows": self.sample_n, "engine": self.engine,
            "repairs": t.repairs.to_dict(), "columns": profiles,
            "duplicates": self._duplicates(cols) if config.EDA_DUPLICATES else None,
            "correlations": self._correlations(profiles) if config.EDA_MULTIVARIATE else [],
            "missing_patterns": self._missing_patterns(cols),
        }
        total_cells = self.n * len(cols)
        miss = sum(p["missing"] for p in profiles)
        result["missing_cells"] = miss
        result["missing_cells_pct"] = _pct(miss, total_cells)
        tc = {}
        for p in profiles:
            tc[p["inferred_type"]] = tc.get(p["inferred_type"], 0) + 1
        result["type_counts"] = tc
        result["n_warnings"] = sum(1 for p in profiles for f in p["flags"] if f["level"] == "warn")
        result["table_flags"] = self._table_flags(profiles)
        self.con.close()
        return result

    def _table_flags(self, profiles) -> list[dict]:
        out = []
        if self.n in (1_048_575, 1_048_576, 65_535, 65_536):
            out.append(_flag("warn", "row_limit", "Exactly {n:,} rows = an Excel sheet row limit: the data was "
                                                  "probably cut off when exported through Excel.", n=self.n))
        periods = [p["name"] for p in profiles if PERIOD_NAME_RE.match(p["name"].split(" / ")[-1].strip())]
        if len(periods) >= 4 and len(periods) >= 0.3 * len(profiles):
            out.append(_flag("info", "wide_format",
                             "{n} columns are time periods ({cols}, …): the table is in a wide / pivoted layout.",
                             n=len(periods), cols=", ".join(periods[:4])))
        if 2 <= self.n <= 30 and len(profiles) >= 2 * self.n and profiles:
            first = profiles[0]
            if first["inferred_type"] in ("text", "categorical") and first["distinct"] == self.n:
                out.append(_flag("info", "transposed", "Few rows, many columns and a unique label column: the table "
                                                       "may be turned sideways (records stored as columns)."))
        return out

    engine = "duckdb"

    def _setup(self) -> list[dict]:
        """Register views `raw`/`b` (full data) and temp table `s` (sample)."""
        con, t = self.con, self.t
        con.execute(f"CREATE VIEW raw AS SELECT * FROM {parquet_src([t.path] + list(t.extra_paths))}")
        desc = [d for d in self.q("DESCRIBE raw") if d[0] not in set(t.drop_columns)]
        self.n = self.q1("SELECT count(*) FROM raw")[0]
        self.huge = self.n > config.HUGE_ROWS
        self.qfn = "approx_quantile" if self.huge else "quantile_cont"
        cols = []
        exprs = []
        for name, typ, *_ in desc:
            k = _kind(typ)
            if k == "other":
                exprs.append(f"CAST({ident(name)} AS VARCHAR) AS {ident(name)}")
                k = "str"
            else:
                exprs.append(ident(name))
            cols.append({"name": name, "physical_type": typ, "kind": k})
        con.execute(f"CREATE VIEW b AS SELECT {', '.join(exprs)} FROM raw")
        sample_n = max(1000, min(config.SAMPLE_ROWS, config.SAMPLE_CELLS // max(1, len(cols))))
        self.progress(0.52, f"Sampling {min(sample_n, self.n):,} rows for type inference")
        if self.n <= sample_n:
            con.execute("CREATE TEMP TABLE s AS SELECT * FROM b")
        else:
            con.execute(f"CREATE TEMP TABLE s AS SELECT * FROM b USING SAMPLE reservoir({sample_n} ROWS) REPEATABLE (42)")
        self.sample_n = min(sample_n, self.n)
        return cols

    # ------------------------------------------------------------------
    @staticmethod
    def nullish(c: dict) -> str:
        x = ident(c["name"])
        if c["kind"] == "str":
            toks = ", ".join(lit(s) for s in NULL_TOKENS)
            return f"({x} IS NULL OR trim({x}) = '' OR lower(trim({x})) IN ({toks}))"
        if c["physical_type"].upper() in ("DOUBLE", "FLOAT", "REAL"):
            return f"({x} IS NULL OR isnan({x}))"
        return f"({x} IS NULL)"

    def _pass_basic(self, cols):
        """One scan per batch of columns: nulls, placeholder nulls, distinct, length, whitespace."""
        for start in range(0, len(cols), 40):
            batch = cols[start:start + 40]
            parts = []
            for c in batch:
                x = ident(c["name"])
                parts += [f"count(*) FILTER (WHERE {self.nullish(c)})",
                          f"count(*) FILTER (WHERE {x} IS NULL)",
                          f"approx_count_distinct({x})" if self.n > 5_000_000 else f"count(DISTINCT {x})"]
                if c["kind"] == "str":
                    parts += [f"min(length({x}))", f"max(length({x}))", f"avg(length({x}))",
                              f"count(*) FILTER (WHERE {x} <> trim({x}) AND trim({x}) <> '')",
                              f"count(*) FILTER (WHERE regexp_matches({x}, {lit(INVISIBLE_SQL)}))",
                              f"count(*) FILTER (WHERE contains({x}, chr(10)) OR contains({x}, chr(13)))",
                              f"count(*) FILTER (WHERE regexp_matches({x}, {lit(MOJIBAKE_SQL)}))"]
                else:
                    parts += ["NULL", "NULL", "NULL", "0", "0", "0", "0"]
            row = self.q1(f"SELECT {', '.join(parts)} FROM b")
            for j, c in enumerate(batch):
                v = row[j * 10:(j + 1) * 10]
                c.update(missing=v[0], hard_null=v[1], distinct_approx=v[2],
                         len_min=v[3], len_max=v[4], len_avg=_py(v[5]), whitespace=v[6] or 0,
                         invisible=v[7] or 0, multiline=v[8] or 0, mojibake=v[9] or 0)

    # ------------------------------------------------------------------
    def _infer_str(self, c) -> dict:
        x = ident(c["name"])
        nl = self.nullish(c)
        bools = ", ".join(lit(s) for s in TRUE_TOKENS + FALSE_TOKENS)
        sem = ", ".join(f"count(*) FILTER (WHERE regexp_full_match(v, {lit(r)}))" for r in SEMANTIC_RE.values())
        r = self.q1(f"""
            WITH v AS (SELECT trim({x}) AS v FROM s WHERE NOT {nl})
            SELECT count(*), count(TRY_CAST(v AS DOUBLE)),
              count(*) FILTER (WHERE regexp_full_match(v, {lit(FMT_NUM_RE)})),
              count(*) FILTER (WHERE regexp_matches(v, {lit(EU_STYLE_RE)})),
              count(*) FILTER (WHERE regexp_matches(v, {lit(US_STYLE_RE)})),
              count(*) FILTER (WHERE regexp_full_match(v, '[-+]?\\d+([.,]\\d+)?\\s*[A-Za-z%°µ/]{{1,6}}')),
              count(*) FILTER (WHERE lower(v) IN ({bools})),
              count(DISTINCT lower(v)),
              count(TRY_CAST(v AS TIMESTAMP)),
              count(*) FILTER (WHERE regexp_matches(v, '^\\d{{1,4}}[-/. ]([A-Za-z]{{3,9}}|\\d{{1,2}})[-/. ,]+\\d{{1,4}}')
                                  OR regexp_matches(v, '^[A-Za-z]{{3,9}} \\d{{1,2}}, \\d{{4}}')),
              count(*) FILTER (WHERE regexp_full_match(v, '0\\d+')),
              avg(len(string_split(v, ' '))),
              count(DISTINCT v),
              count(*) FILTER (WHERE regexp_full_match(v, '[-+]?\\d(\\.\\d+)?[eE]\\+\\d+')),
              count(*) FILTER (WHERE regexp_full_match(v, '[-+]?[1-9]\\d{{0,2}}(\\.\\d{{3}})+')),
              count(*) FILTER (WHERE regexp_full_match(v, '[-+]?\\d+')),
              count(*) FILTER (WHERE regexp_full_match(v, '[-+]?[1-9]\\d{{0,2}}\\.\\d{{1,2}}')),
              {sem}
            FROM v""")
        keys = ["nn", "dbl", "numfmt", "eu", "us", "unit", "bool", "distinct_lower", "ts", "datey", "lead0", "words",
                "distinct", "sci", "dot3", "int", "dotshort"]
        out = dict(zip(keys, r[:len(keys)]))
        out["semantic"] = dict(zip(SEMANTIC_RE.keys(), r[len(keys):]))
        return out

    def _best_date_format(self, c, nn) -> tuple[str | None, int, bool]:
        """Best-matching date format, its hit count, and whether the day/month
        order is ambiguous (the swapped format fits the sample almost as well)."""
        x = ident(c["name"])
        parts = ", ".join(f"count(try_strptime(trim({x}), {lit(f)}))" for f in DATE_FORMATS)
        r = self.q1(f"SELECT count(TRY_CAST(trim({x}) AS TIMESTAMP)), {parts} FROM s WHERE NOT {self.nullish(c)}")
        best_i = max(range(len(r)), key=lambda i: r[i])
        fmt = None if best_i == 0 else DATE_FORMATS[best_i - 1]
        ambiguous = False
        if fmt and "%d" in fmt and "%m" in fmt:
            swapped = fmt.replace("%d", "\0").replace("%m", "%d").replace("\0", "%m")
            if swapped in DATE_FORMATS:
                ambiguous = r[DATE_FORMATS.index(swapped) + 1] >= 0.9 * r[best_i]
        return fmt, r[best_i], ambiguous

    def _decide_type(self, c) -> dict:
        """Return {type, conv, note, ...}. `conv` names the conversion from the raw
        column to a typed value; conv_expr() renders it in the engine's SQL dialect."""
        learned = (getattr(self.t, "type_hints", None) or {}).get(c["name"])
        if learned and c["kind"] == "str":       # user feedback: keep as text / ID / category
            c["_inf"] = self._infer_str(c)
            return {"type": "text", "note": "Kept as text (learned from user feedback).",
                    "code": learned == "identifier"}
        if c["kind"] == "num":
            return {"type": "numeric", "conv": "num_native"}
        if c["kind"] == "bool":
            return {"type": "boolean", "conv": "bool_native"}
        if c["kind"] == "date":
            return {"type": "datetime", "conv": "date_native"}
        inf = self._infer_str(c)
        c["_inf"] = inf
        nn = inf["nn"]
        if nn == 0:
            return {"type": "empty"}
        th = 0.8 * nn
        if inf["bool"] >= 0.98 * nn and inf["distinct_lower"] <= 2:
            return {"type": "boolean", "conv": "bool_str"}
        # '.' as thousands separator next to plain integers (397.278 beside 9002), common in
        # Indonesian / European exports; '47.19' there is 47.190 with the trailing zero dropped
        if inf["dot3"] >= max(2, 0.2 * nn) and inf["int"] >= 1 and inf["dotshort"] <= 0.25 * nn \
                and inf["dot3"] + inf["int"] + inf["dotshort"] >= 0.95 * nn:
            return {"type": "numeric", "conv": "num_dot3", "short_groups": inf["dotshort"],
                    "note": "'.' is a thousands separator in this column (397.278 = 397,278)."}
        if inf["lead0"] >= 0.1 * nn and inf["dbl"] >= th:
            return {"type": "text", "note": "Numeric-looking code with leading zeros: kept as text.", "code": True}
        if inf["dbl"] >= th and inf["dbl"] >= inf["numfmt"]:
            return {"type": "numeric", "conv": "double", "note": "Numbers stored as text."}
        if inf["numfmt"] >= th:
            eu = inf["eu"] > inf["us"]
            return {"type": "numeric", "conv": "num_eu" if eu else "num_us",
                    "note": ("Formatted numbers converted: " +
                             ("'.' thousands / ',' decimal (e.g. 1.234,56)" if eu else "',' thousands / '.' decimal") +
                             "; currency symbols, %, (negatives) and footnote marks handled.")}
        if inf["unit"] >= th:
            units = self.q(f"SELECT lower(regexp_extract(trim({ident(c['name'])}), '[A-Za-z%°µ/]+$')) u, count(*) FROM s "
                           f"WHERE NOT {self.nullish(c)} GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
            return {"type": "numeric", "conv": "unit",
                    "note": "Value and unit merged in one field; numeric part extracted.",
                    "units": [[u, n] for u, n in units if u]}
        if inf["ts"] >= th or inf["datey"] >= 0.5 * nn:
            fmt, k, ambiguous = self._best_date_format(c, nn)
            if k >= th:
                if fmt is None:
                    return {"type": "datetime", "conv": "ts", "note": "Dates stored as text."}
                return {"type": "datetime", "conv": "ts_fmt", "fmt": fmt, "ambiguous": ambiguous,
                        "note": "Dates stored as text, format detected: {fmt}", "note_p": {"fmt": fmt}}
        return {"type": "text"}

    def conv_expr(self, d, c) -> str:
        x = ident(c["name"])
        v = f"trim({x})"
        t = ", ".join(lit(s) for s in TRUE_TOKENS)
        f = ", ".join(lit(s) for s in FALSE_TOKENS)
        return {
            "num_native": f"CAST({x} AS DOUBLE)",
            "bool_native": x,
            "date_native": f"CAST({x} AS TIMESTAMP)",
            "bool_str": f"CASE WHEN lower({v}) IN ({t}) THEN true WHEN lower({v}) IN ({f}) THEN false END",
            "double": f"TRY_CAST({v} AS DOUBLE)",
            "num_us": f"({NEG_SIGN.format(v=v)} * TRY_CAST(regexp_replace({v}, '[^0-9.]', '', 'g') AS DOUBLE))",
            "num_eu": f"({NEG_SIGN.format(v=v)} * TRY_CAST(replace(regexp_replace({v}, '[^0-9,]', '', 'g'), ',', '.') AS DOUBLE))",
            "unit": f"TRY_CAST(replace(regexp_extract({v}, '^[-+]?\\d+([.,]\\d+)?'), ',', '.') AS DOUBLE)",
            # 397.278 -> 397278; 47.19 -> 47190 (trailing zero dropped)
            "num_dot3": f"TRY_CAST(CASE WHEN regexp_full_match({v}, '[-+]?[1-9]\\d{{0,2}}\\.\\d{{1,2}}') "
                        f"THEN split_part({v}, '.', 1) || rpad(split_part({v}, '.', 2), 3, '0') "
                        f"ELSE replace({v}, '.', '') END AS DOUBLE)",
            "ts": f"TRY_CAST({v} AS TIMESTAMP)",
            "ts_fmt": f"COALESCE(TRY_CAST({v} AS TIMESTAMP), try_strptime({v}, {lit(d.get('fmt', ''))}))",
        }.get(d.get("conv"), x)

    # ------------------------------------------------------------------
    def _profile_column(self, idx, c) -> dict:
        n = self.n
        d = self._decide_type(c)
        d["expr"] = self.conv_expr(d, c)
        p = {
            "index": idx, "name": c["name"], "physical_type": c["physical_type"],
            "inferred_type": d["type"], "semantic": None, "count": n,
            "missing": c["missing"], "missing_pct": _pct(c["missing"], n),
            "null_tokens": c["missing"] - c["hard_null"],
            "distinct": c["distinct_approx"], "distinct_pct": _pct(c["distinct_approx"], n - c["missing"]),
            "numeric": None, "datetime": None, "text": None, "boolean": None,
            "histogram": None, "top_values": [], "top_approx": False, "flags": [], "notes": [],
        }
        src = (getattr(self.t, "derived", None) or {}).get(c["name"])
        if src:
            p["derived_from"] = src
            p["notes"].append(msg("Normalised copy of column '{c}' with merged cells filled in.", c=src))
        p["sample"] = self._sample(c)
        if d.get("note"):
            p["notes"].append(msg(d["note"], **d.get("note_p", {})))
        if d.get("units"):
            p["units"] = d["units"]
        nn = n - c["missing"]
        flags = p["flags"]
        if nn == 0:
            p["inferred_type"] = "empty"
            flags.append(_flag("warn", "empty", "Column is completely empty."))
            return p

        if d["type"] == "numeric":
            self._numeric(c, d, p)
        elif d["type"] == "datetime":
            self._datetime(c, d, p)
        elif d["type"] == "boolean":
            self._boolean(c, d, p)
        else:
            self._text(c, d, p)

        # ---- generic quality flags
        mp = p["missing_pct"]
        if mp >= 50:
            flags.append(_flag("warn", "missing", "{pct:.1f}% of values are empty.", pct=mp))
        elif mp >= 5:
            flags.append(_flag("info", "missing", "{pct:.1f}% of values are empty.", pct=mp))
        if p["null_tokens"]:
            flags.append(_flag("info", "null_tokens",
                               "{n:,} placeholder values (e.g. 'N/A', '-', 'null') counted as empty.", n=p["null_tokens"]))
        if p["distinct"] <= 1:
            flags.append(_flag("warn", "constant", "Every row has the same value."))
        if c.get("whitespace"):
            flags.append(_flag("info", "whitespace", "{n:,} values have extra spaces at the start or end.", n=c["whitespace"]))
        if p["top_values"] and p["distinct"] > 1:
            top = p["top_values"][0]
            if top[2] >= 95 and p["inferred_type"] not in ("boolean",):
                flags.append(_flag("info", "imbalance", "Almost all rows have the same value ('{v}' = {pct:.1f}%).", v=top[0], pct=top[2]))
        if c.get("invisible"):
            flags.append(_flag("warn", "invisible_chars",
                               "{n:,} values contain invisible characters (special spaces or control characters).",
                               n=c["invisible"]))
        if c.get("multiline"):
            flags.append(_flag("info", "line_breaks", "{n:,} values contain line breaks (Enter inside a cell).", n=c["multiline"]))
        if c.get("mojibake"):
            flags.append(_flag("warn", "mojibake", "{n:,} values have garbled characters (e.g. 'Ã©' instead of 'é').",
                               n=c["mojibake"]))
        inf = c.get("_inf") or {}
        if inf.get("sci") and inf["sci"] >= 0.05 * max(1, inf["nn"]):
            flags.append(_flag("warn", "scientific_notation",
                               "{pct:.0f}% of values are in scientific notation (e.g. 1.23E+15).",
                               pct=_pct(inf["sci"], inf["nn"])))
        inf = c.get("_inf") or {}
        if d.get("conv") == "double" and inf.get("dot3", 0) >= 0.8 * inf.get("nn", 1) and not inf.get("int"):
            # every value has three digits after '.': decimals, or thousands written as 55.315?
            flags.append(_flag("warn", "maybe_thousands",
                               "Almost every value has exactly 3 digits after '.', e.g. {ex}: '.' may be a thousands "
                               "separator (55.315 = 55,315); values were read as decimals.",
                               ex=next(iter(p.get("top_values") or []), [""])[0]))
        if d.get("short_groups"):
            col = f"trim({ident(c['name'])})"
            ex = [[v, None] for (v,) in self.q(
                f"SELECT DISTINCT {col} FROM s WHERE regexp_full_match({col}, '[-+]?[1-9]\\d{{0,2}}\\.\\d{{1,2}}') LIMIT 3")]
            flags.append(_flag("warn", "thousands_ambiguous",
                               "{n} value(s) have fewer than 3 digits after the thousands dot; read as if a trailing "
                               "zero was dropped (47.19 = 47,190).", ex, n=d["short_groups"]))
        if d.get("ambiguous"):
            flags.append(_flag("warn", "ambiguous_dates", "Day and month could be swapped (most days are 12 or less); "
                                                          "assumed format {fmt}.", fmt=d["fmt"]))
        m = UNIT_HEADER_RE.search(c["name"])
        if m:
            p["notes"].append(msg("The column name mentions a unit or scale: '{u}'.", u=m.group(0).strip()))
        if p.get("sample") is not None and p["inferred_type"] in ("categorical", "text") and p["distinct"] > 10:
            p["sample"]["rare"] = self._rare_values(c, p)
        p["issue_rows"] = self._issue_rows(c, d, p)
        return p

    @contextlib.contextmanager
    def _ordered(self):
        """Row order matters for head / tail / row numbers; the profiling scans do not need it."""
        self.con.execute("SET preserve_insertion_order=true")
        try:
            yield
        finally:
            self.con.execute("SET preserve_insertion_order=false")

    def _sample(self, c, k=10) -> dict | None:
        """First and last k rows of the column exactly as stored (empty cells included, with row
        numbers), plus k values picked at random from the non-empty ones."""
        x = f"CAST({ident(c['name'])} AS VARCHAR)"
        clip = lambda v: None if v is None else (v if len(v) <= 80 else v[:79] + "…")  # noqa: E731
        try:
            with self._ordered():
                head = self.q(f"SELECT {x} FROM b LIMIT {k}")
                tail_from = max(len(head), self.n - k)
                tail = self.q(f"SELECT {x} FROM b LIMIT {k} OFFSET {tail_from}") if self.n > len(head) else []
            rnd = self.q(f"SELECT v FROM (SELECT {x} v FROM s WHERE NOT {self.nullish(c)}) "
                         f"USING SAMPLE reservoir({k} ROWS) REPEATABLE (7)") if self.n > 2 * k else []
        except Exception:
            return None
        return {"head": [[i + 1, clip(v)] for i, (v,) in enumerate(head)],
                "tail": [[tail_from + i + 1, clip(v)] for i, (v,) in enumerate(tail)],
                "random": [clip(v) for (v,) in rnd]}

    def _rare_values(self, c, p, k=10) -> list:
        """Least frequent values (sample): typos and one-off spellings hide here."""
        try:
            return [[v, n] for v, n in self.q(
                f"SELECT trim(CAST({ident(c['name'])} AS VARCHAR)) v, count(*) n FROM s WHERE NOT {self.nullish(c)} "
                f"GROUP BY 1 HAVING count(*) <= 2 ORDER BY 2, 1 LIMIT {k}")]
        except Exception:
            return []

    ISSUE_COND = {   # flag code -> SQL condition on the raw text column
        "null_tokens": "{nl} AND {x} IS NOT NULL AND trim({x}) <> ''",
        "whitespace": "{x} <> trim({x}) AND trim({x}) <> ''",
        "invisible_chars": "regexp_matches({x}, {inv})",
        "mojibake": "regexp_matches({x}, {moj})",
        "line_breaks": "contains({x}, chr(10)) OR contains({x}, chr(13))",
    }

    def _issue_rows(self, c, d, p, k=5) -> dict:
        """For each flagged issue, the first rows (row number in the cleaned table + the raw value)."""
        x, out = ident(c["name"]), {}
        conds = {}
        codes = {f["code"] for f in p["flags"]}
        if c["kind"] == "str":
            for code, tpl in self.ISSUE_COND.items():
                if code in codes:
                    conds[code] = tpl.format(x=x, nl=self.nullish(c), inv=lit(INVISIBLE_SQL), moj=lit(MOJIBAKE_SQL))
        if "mixed_type" in codes and d.get("expr") and d["type"] in ("numeric", "datetime"):
            conds["mixed_type"] = f"NOT {self.nullish(c)} AND ({d['expr']}) IS NULL"
        if not conds:
            return out
        try:
            with self._ordered():
                for code, cond in conds.items():
                    rows = self.q(f"SELECT rn, CAST({x} AS VARCHAR) FROM (SELECT row_number() OVER () AS rn, {x} FROM b) "
                                  f"WHERE {cond} ORDER BY rn LIMIT {k}")
                    if rows:
                        out[code] = [[rn, v if v is None or len(v) <= 80 else v[:79] + "…"] for rn, v in rows]
        except Exception:
            pass
        return out

    def _missing_patterns(self, cols) -> dict | None:
        """Which columns are empty together: the most common combinations of empty columns, and pairs
        that are (almost) always empty on the same rows (structural blanks rather than random ones)."""
        cand = sorted((c for c in cols if 0 < c["missing"] < self.n), key=lambda c: -c["missing"])[:12]
        if len(cand) < 2:
            return None
        try:
            nls = [self.nullish(c) for c in cand]
            bits = ", ".join(f"CASE WHEN {nl} THEN '1' ELSE '0' END" for nl in nls)
            pats = self.q(f"SELECT concat({bits}) p, count(*) n FROM b GROUP BY 1 ORDER BY 2 DESC LIMIT 8")
            pairs = [(i, j) for i in range(len(cand)) for j in range(i + 1, len(cand))]
            r = self.q1("SELECT " + ", ".join(
                f"count(*) FILTER (WHERE {nls[i]} AND {nls[j]}), count(*) FILTER (WHERE {nls[i]} OR {nls[j]})"
                for i, j in pairs) + " FROM b")
        except Exception:
            return None
        together = []
        for k, (i, j) in enumerate(pairs):
            both, either = r[2 * k], r[2 * k + 1]
            if either and both / either >= 0.9 and both >= 2:
                together.append([cand[i]["name"], cand[j]["name"], _pct(both, either)])
        together.sort(key=lambda t: -t[2])
        return {"cols": [c["name"] for c in cand], "patterns": [[b, n, _pct(n, self.n)] for b, n in pats],
                "together": together[:6]}

    def _top(self, c, expr_sql=None, k=10, use_sample=False):
        x = ident(c["name"])
        src = "s" if use_sample else "b"
        key = expr_sql or x
        rows = self.q(f"SELECT CAST({key} AS VARCHAR) v, count(*) n FROM {src} WHERE NOT {self.nullish(c)} "
                      f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {k}")
        base = (self.sample_n - self.q1(f"SELECT count(*) FROM s WHERE {self.nullish(c)}")[0]) if use_sample \
            else self.n - c["missing"]
        return [[v, n, _pct(n, base)] for v, n in rows]

    def _need_sample_top(self, c):
        return self.huge or c["distinct_approx"] > 1_000_000

    def _invalid_examples(self, c, d):
        x = ident(c["name"])
        return [[v, n] for v, n in self.q(
            f"SELECT CAST({x} AS VARCHAR), count(*) FROM b WHERE NOT {self.nullish(c)} AND ({d['expr']}) IS NULL "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT 5")]

    # ---- numeric -----------------------------------------------------
    def _numeric(self, c, d, p):
        e, nl = d["expr"], self.nullish(c)
        r = self.q1(f"""SELECT count(x), min(x), max(x), avg(x), stddev_samp(x), {self.qfn}(x, {QUANTS}),
              skewness(x), kurtosis(x), count(*) FILTER (WHERE x = 0), count(*) FILTER (WHERE x < 0),
              count(*) FILTER (WHERE x <> floor(x)), count(*) FILTER (WHERE NOT nl AND x IS NULL), sum(x)
            FROM (SELECT {e} AS x, {nl} AS nl FROM b) WHERE x IS NULL OR isfinite(x)""")
        cnt, lo, hi, mean, sd, qs, skew, kurt, zeros, neg, frac, invalid, total = [_py(v) for v in r]
        if not cnt:
            p["inferred_type"] = "text"
            self._text(c, {"type": "text"}, p)
            return
        qd = dict(zip(["p1", "p5", "q1", "median", "q3", "p95", "p99"], qs or [None] * 7))
        is_int = frac == 0
        p["inferred_type"] = "integer" if is_int else "float"
        num = {"count": cnt, "min": lo, "max": hi, "mean": mean, "std": sd, "skew": skew, "kurtosis": kurt,
               "zeros": zeros, "negatives": neg, "sum": total, **qd,
               "cv": (sd / abs(mean)) if sd is not None and mean else None}
        iqr = (qd["q3"] - qd["q1"]) if qd["q1"] is not None else 0
        lof, hif = qd["q1"] - 1.5 * iqr, qd["q3"] + 1.5 * iqr
        o = self.q1(f"SELECT count(*) FILTER (WHERE x < {lof} OR x > {hif}) FROM (SELECT {e} AS x FROM b) WHERE isfinite(x)")[0]
        num.update(outliers=o, outlier_low=lof, outlier_high=hif, outlier_pct=_pct(o, cnt))
        p["numeric"] = num
        p["histogram"] = self._num_hist(e, lo, hi, is_int)
        distinct = c["distinct_approx"]
        if is_int and distinct <= 50 or distinct <= 20:
            p["top_values"] = self._top(c, None, 10, self._need_sample_top(c))
        exact = self.n <= 5_000_000   # above this, distinct is a HyperLogLog estimate (~2-3% error)
        if is_int and distinct >= (0.99 if exact else 0.95) * cnt and cnt >= 20:
            p["semantic"] = "identifier"
        elif is_int and distinct == 2 and lo == 0 and hi == 1:
            p["semantic"] = "binary flag"
        elif is_int and distinct <= 15:
            p["semantic"] = "discrete / code"
        flags = p["flags"]
        if invalid:
            ex = self._invalid_examples(c, d)
            flags.append(_flag("warn", "mixed_type",
                               "{n:,} non-numeric values ({pct:.1f}%) in a numeric column.",
                               n=invalid, pct=_pct(invalid, cnt + invalid),
                               examples=ex))
        if o and p["semantic"] != "identifier":
            flags.append(_flag("info", "outliers", "{n:,} unusually small or large values ({pct:.2f}%) outside the "
                                                   "typical range [{lo}, {hi}].", n=o, pct=_pct(o, cnt),
                               lo=float(lof), hi=float(hif)))
        if skew is not None and abs(skew) >= 2 and p["semantic"] != "identifier":
            flags.append(_flag("info", "skew", ("Most values are small with a few very large ones (right-skewed, skew {s:.2f})."
                                          if skew > 0 else
                                          "Most values are large with a few very small ones (left-skewed, skew {s:.2f})."),
                               s=skew))
        if neg and neg < 0.01 * cnt and lo is not None and qd["p5"] is not None and qd["p5"] >= 0:
            flags.append(_flag("info", "rare_negatives", "{n:,} negative values in a column that is otherwise positive.", n=neg))
        if (SERIAL_DATE_NAME_RE.search(c["name"]) and lo is not None and 20000 <= lo and hi <= 80000
                and p["semantic"] != "identifier"):
            ex = dt.date(1899, 12, 30) + dt.timedelta(days=int(qd["median"] or lo))
            flags.append(_flag("warn", "excel_serial_dates",
                               "Looks like dates stored as Excel serial numbers (e.g. {num} = {date}).",
                               num=int(qd["median"] or lo), date=str(ex)))
        for sentinel in (-1, 999, 9999, 99999, -999):
            if lo == sentinel or hi == sentinel:
                k = self.q1(f"SELECT count(*) FROM (SELECT {e} AS x FROM b) WHERE x = {sentinel}")[0]
                if k > 0.001 * cnt and p["semantic"] != "identifier":
                    flags.append(_flag("info", "sentinel", "Possible placeholder value {v} appears {n:,} times.", v=sentinel, n=k))

    def _num_hist(self, e, lo, hi, is_int, nb=24):
        if lo is None or hi is None:
            return None
        if lo == hi:
            n = self.q1(f"SELECT count(*) FROM (SELECT {e} AS x FROM b) WHERE x = {lo}")[0]
            return {"kind": "numeric", "bins": [[_fmt(lo), n]]}
        if is_int and hi - lo < 40:
            w, nb = 1.0, int(hi - lo) + 1
        else:
            w = (hi - lo) / nb
        rows = dict(self.q(f"SELECT LEAST(FLOOR((x - {lo}) / {w})::BIGINT, {nb - 1}) b, count(*) "
                           f"FROM (SELECT {e} AS x FROM b) WHERE x IS NOT NULL AND isfinite(x) GROUP BY 1"))
        bins = []
        for i in range(nb):
            a = lo + i * w
            label = _fmt(a) if w == 1.0 and is_int else f"{_fmt(a)} – {_fmt(a + w)}"
            bins.append([label, rows.get(i, 0)])
        return {"kind": "numeric", "bins": bins}

    # ---- datetime ------------------------------------------------------
    def _datetime(self, c, d, p):
        e, nl = d["expr"], self.nullish(c)
        r = self.q1(f"""SELECT count(x), min(x), max(x), count(*) FILTER (WHERE x > now()::TIMESTAMP),
              count(*) FILTER (WHERE x < TIMESTAMP '1900-01-01'), count(*) FILTER (WHERE CAST(x AS TIME) <> TIME '00:00:00'),
              count(*) FILTER (WHERE NOT nl AND x IS NULL), {self.qfn}(epoch(x), 0.5)
            FROM (SELECT CAST({e} AS TIMESTAMP) AS x, {nl} AS nl FROM b)""")
        cnt, lo, hi, future, ancient, with_time, invalid, med = r
        if not cnt:
            p["inferred_type"] = "text"
            self._text(c, {"type": "text"}, p)
            return
        span = (hi - lo).days
        p["inferred_type"] = "datetime" if with_time else "date"
        p["datetime"] = {"count": cnt, "min": _py(lo), "max": _py(hi), "span_days": span, "future": future,
                         "before_1900": ancient, "has_time": bool(with_time),
                         "median": _py(dt.datetime.fromtimestamp(med, dt.timezone.utc).replace(tzinfo=None)) if med is not None else None}
        grain = "year" if span > 3 * 365 else "month" if span > 90 else "day"
        fmt = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}[grain]
        rows = self.q(f"SELECT strftime(date_trunc('{grain}', x), '{fmt}') b, count(*) FROM "
                      f"(SELECT CAST({e} AS TIMESTAMP) AS x FROM b) WHERE x IS NOT NULL GROUP BY 1 ORDER BY 1")
        if len(rows) > 120:  # compress very long ranges
            step = math.ceil(len(rows) / 120)
            rows = [[rows[i][0], sum(n for _, n in rows[i:i + step])] for i in range(0, len(rows), step)]
        p["histogram"] = {"kind": "date", "grain": grain, "bins": [[b, n] for b, n in rows]}
        wd = self.q(f"SELECT dayofweek(x), count(*) FROM (SELECT CAST({e} AS TIMESTAMP) AS x FROM b) "
                    f"WHERE x IS NOT NULL GROUP BY 1 ORDER BY 1")
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        p["datetime"]["weekday"] = [[names[int(k)], n] for k, n in wd]
        flags = p["flags"]
        if invalid:
            flags.append(_flag("warn", "mixed_type", "{n:,} values could not be read as dates.", n=invalid,
                               examples=self._invalid_examples(c, d)))
        if future:
            flags.append(_flag("info", "future_dates", "{n:,} dates are in the future.", n=future))
        if ancient:
            flags.append(_flag("warn", "old_dates", "{n:,} dates before 1900 (possible placeholder like 1900-01-01).", n=ancient))

    # ---- boolean -------------------------------------------------------
    def _boolean(self, c, d, p):
        r = self.q1(f"SELECT count(*) FILTER (WHERE x), count(*) FILTER (WHERE NOT x) FROM (SELECT {d['expr']} AS x FROM b)")
        p["boolean"] = {"true": r[0], "false": r[1]}
        p["top_values"] = self._top(c, None, 6)
        p["histogram"] = {"kind": "category", "bins": [["true", r[0]], ["false", r[1]]]}

    # ---- text ----------------------------------------------------------
    def _text(self, c, d, p):
        x = ident(c["name"])
        inf = c.get("_inf") or self._infer_str(c) if c["kind"] == "str" else {}
        nn = self.n - c["missing"]
        distinct = c["distinct_approx"]
        sem = None
        if inf:
            for k, v in inf["semantic"].items():
                if v >= 0.8 * inf["nn"]:
                    sem = k
                    break
        avg_len = c.get("len_avg") or 0
        words = (inf.get("words") or 1) if inf else 1
        if distinct <= 1:
            sub = "categorical"
        elif d.get("code"):
            sub = "code"
        elif distinct <= 50 or (nn > 1000 and distinct / nn < 0.02):
            sub = "categorical"
        elif avg_len >= 40 or words >= 6:
            sub = "free text"
        elif distinct >= (0.98 if self.n <= 5_000_000 else 0.95) * nn:
            sub = "identifier"
        else:
            sub = "text"
        p["inferred_type"] = "categorical" if sub == "categorical" else "text"
        p["semantic"] = sem or (sub if sub in ("identifier", "free text", "code") else None)
        learned = (getattr(self.t, "type_hints", None) or {}).get(c["name"])
        if learned == "identifier":
            p["semantic"] = "identifier"
        elif learned == "category":
            p["inferred_type"] = "categorical"
        p["text"] = {"len_min": c.get("len_min"), "len_max": c.get("len_max"), "len_avg": avg_len,
                     "avg_words": words, "subtype": sub}
        p["top_values"] = self._top(c, None, 10, self._need_sample_top(c))
        p["top_approx"] = self._need_sample_top(c)
        if sub in ("categorical", "text", "code"):
            # value frequency list (sample) for the AI anomaly review
            p["value_list"] = [[v, n] for v, n in self.q(
                f"SELECT trim({x}) v, count(*) n FROM s WHERE NOT {self.nullish(c)} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 150")]
        if sub == "categorical" and p["top_values"]:
            p["histogram"] = {"kind": "category", "bins": [[v, n] for v, n, _ in p["top_values"]]}
        nl = self.nullish(c)
        pats = self.q(f"""SELECT left(regexp_replace(regexp_replace(regexp_replace(trim({x}), '[A-Z]', 'A', 'g'),
                     '[a-z]', 'a', 'g'), '[0-9]', '9', 'g'), 40) pat, count(*) n FROM s WHERE NOT {nl}
                     GROUP BY 1 ORDER BY 2 DESC LIMIT 6""")
        sn = sum(n for _, n in pats) or 1
        if sub != "free text":
            p["text"]["patterns"] = [[pt, n, _pct(n, self.sample_n - self.q1(f"SELECT count(*) FROM s WHERE {nl}")[0])]
                                     for pt, n in pats]
        flags = p["flags"]
        if sub == "categorical" and distinct > 1:
            groups = self.q(f"SELECT list(DISTINCT {x}) FROM s WHERE NOT {nl} GROUP BY lower(trim({x})) "
                            f"HAVING count(DISTINCT {x}) > 1 ORDER BY count(*) DESC")
            if groups:
                ex = [[" / ".join(repr(v) for v in g[0][:3]), len(g[0])] for g in groups[:3]]
                flags.append(_flag("warn", "case_variants",
                                   "{n} values are written in several ways (upper/lower case or spaces), e.g. {ex}.",
                                   examples=ex, n=len(groups), ex=ex[0][0]))
        if inf and sub in ("text", "categorical", "identifier", "code"):
            m = self._merged(c, inf)
            if m:
                p["merged"] = m
                flags.append(_flag("info" if m["sep"] == " " else "warn", "merged_column",
                                   "Each value seems to hold {k} pieces of information joined by {sep} ({pct:.0f}% of values).",
                                   examples=m["examples"], k=m["parts"], sep=repr(m["sep"]), pct=m["pct"]))
        if inf and inf["dbl"] and 0.3 * inf["nn"] <= inf["dbl"] < 0.8 * inf["nn"]:
            flags.append(_flag("warn", "mixed_type", "Mixed content: {pct:.0f}% numbers, the rest text.",
                               pct=_pct(inf["dbl"], inf["nn"])))

    def _merged(self, c, inf):
        """Detect columns that consistently pack several fields into one value."""
        x = ident(c["name"])
        nl = self.nullish(c)
        nn = inf["nn"]
        if nn < 5:
            return None
        for sep in MERGE_SEPS:
            r = self.q1(f"SELECT k, n FROM (SELECT len(string_split(trim({x}), {lit(sep)})) k, count(*) n "
                        f"FROM s WHERE NOT {nl} GROUP BY 1) ORDER BY n DESC LIMIT 1")
            if not r or r[0] < 2 or r[0] > 6 or r[1] < 0.9 * nn:
                continue
            k = r[0]
            num_ratio = self.q1("SELECT " + ", ".join(
                f"count(TRY_CAST(trim(split_part(trim({x}), {lit(sep)}, {i + 1})) AS DOUBLE)) / count(*)"
                for i in range(k)) + f" FROM s WHERE NOT {nl}")
            num_ratio = [float(v or 0) for v in num_ratio]
            mixed_parts = max(num_ratio) >= 0.9 and min(num_ratio) <= 0.1
            if sep == " " and not mixed_parts:
                continue
            if sep in ("_", ":") and not mixed_parts:
                continue
            ex = self.q(f"SELECT trim({x}) FROM s WHERE NOT {nl} AND len(string_split(trim({x}), {lit(sep)})) = {k} LIMIT 3")
            examples = [[e[0], " | ".join(s.strip() for s in e[0].split(sep))] for e in ex]
            return {"sep": sep, "parts": k, "numeric_parts": num_ratio, "examples": examples, "pct": _pct(r[1], nn),
                    "msg": f"Values look like {k} fields merged with separator {sep!r} "
                           f"({_pct(r[1], nn):.0f}% of values); consider splitting."}
        return None

    # ---- dataset-level ---------------------------------------------------
    def _duplicates(self, cols):
        if not cols:
            return None
        h = "hash(" + ", ".join(ident(c["name"]) for c in cols[:200]) + ")"
        if self.huge:
            d = self.q1(f"SELECT approx_count_distinct({h}) FROM b")[0]
            return {"count": max(0, self.n - d), "pct": _pct(max(0, self.n - d), self.n), "approximate": True}
        d = self.q1(f"SELECT count(DISTINCT {h}) FROM b")[0]
        return {"count": self.n - d, "pct": _pct(self.n - d, self.n), "approximate": False}

    def _correlations(self, profiles):
        nums = [p for p in profiles if p["numeric"] and p.get("semantic") != "identifier"][:25]
        if len(nums) < 2:
            return []
        exprs, pairs = [], []
        cols = {p["name"]: p for p in nums}
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                a, b_ = nums[i]["name"], nums[j]["name"]
                exprs.append(f"corr(TRY_CAST({ident(a)} AS DOUBLE), TRY_CAST({ident(b_)} AS DOUBLE))")
                pairs.append((a, b_))
        # numeric-as-text columns need their conversion; plain TRY_CAST covers the common case
        r = self.q1(f"SELECT {', '.join(exprs)} FROM s")
        out = [{"a": a, "b": b_, "r": _py(v)} for (a, b_), v in zip(pairs, r) if v is not None and abs(v) >= 0.5]
        return sorted(out, key=lambda d: -abs(d["r"]))[:15]


def _flag(level, code, tpl, examples=None, **params):
    """A data-quality flag. `tpl` is an English template rendered with `params`
    (translated at report time); `msg` keeps the English text for the LLM / JSON."""
    m = msg(tpl, **params)
    f = {"level": level, "code": code, "msg": m["text"], "m": m}
    if examples:
        f["examples"] = examples
    return f


def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        if v != v:
            return "–"
        a = abs(v)
        if a != 0 and (a >= 1e9 or a < 1e-3):
            return f"{v:.3g}"
        if v.is_integer():
            return f"{int(v):,}"
        return f"{v:,.{2 if a >= 100 else 3}f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def profile_table(table: Table, progress=lambda p, m: None) -> dict:
    return TableProfiler(table, progress).run()
