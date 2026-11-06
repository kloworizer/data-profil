"""End-to-end run: ingest -> profile -> (LLM) -> HTML report."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import cancel, config, memory
from .narrative import describe_column


def run(path: str | Path, out_html: Path, *, job_id: str = "cli", engine: str | None = None,
        use_llm: bool = True, model: str | None = None, max_rows: int = 0, lang: str = "en",
        progress=lambda p, m: None, keep_work: bool = False, report_id: str | None = None) -> dict:
    t0 = time.time()
    config.ensure_dirs()
    from . import settings
    settings.apply()                 # values saved on the Settings page
    engine = engine or config.ENGINE
    path_s = str(path)
    work = config.WORK_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)
    warnings = []
    try:
        from . import spark_engine
        use_spark = engine == "spark"
        if use_spark and not spark_engine.available():
            raise RuntimeError("Spark engine requested but pyspark is not installed (pip install pyspark; needs Java).")
        if use_spark and not spark_engine.handles(path_s):
            warnings.append("This format is not handled by the Spark engine; the local engine was used.")
            use_spark = False
        if use_spark:
            sc = spark_engine.get_spark().sparkContext
            sc.setJobGroup(job_id, f"autoeda {job_id}", interruptOnCancel=True)
            cancel.on_cancel(lambda: sc.cancelJobGroup(job_id))

        # the local model is checked once; it also helps read difficult table layouts
        llm = None
        llm_info = {"used": False, "model": model or config.OLLAMA_MODEL, "lang": lang}
        if use_llm:
            from .llm import Ollama
            llm = Ollama(model)
            if llm.available():
                llm_info["used"] = True
            else:
                warnings.append(f"Ollama model '{llm.model}' not reachable at {llm.host}; "
                                "report uses rule-based descriptions only.")
                llm = None

        # skills learned from earlier feedback on files of this kind
        ctx, skills, hints = {}, [], {}
        if not use_spark and config.MEMORY_ENABLED:
            try:
                from .ingest import sniff_format
                ctx = memory.context_of(Path(path_s), sniff_format(Path(path_s)))
                skills = memory.recall(ctx)
                hints = memory.merge_hints(skills)
            except Exception as e:  # memory never stops a run
                warnings.append(f"Memory not available: {type(e).__name__}: {e}")

        # 1. ingest ------------------------------------------------------
        progress(0.02, "Reading file")
        if use_spark:
            tables = spark_engine.ingest(path_s, job_id, progress, max_rows=max_rows)
            fmt = tables[0].source.get("format", "?")
            size = _size(path_s)
        else:
            from .ingest import ingest
            from .ingest.grid import HEADER_ROWS
            p = Path(path_s)
            token = HEADER_ROWS.set(hints.get("header_rows"))
            try:
                tables, fmt = ingest(p, work, progress, max_rows=max_rows,
                                     llm=llm if config.AI_TABLE_LAYOUT else None)
            finally:
                HEADER_ROWS.reset(token)
            size = p.stat().st_size
        if not tables:
            raise ValueError("No tabular data found in the file.")
        if ctx:
            try:
                ctx["columns"] = [c for t in tables for c in t.columns]
                more = [s for s in memory.recall(ctx) if s["id"] not in {k["id"] for k in skills}]
                skills += more
                hints = memory.merge_hints(skills)
                tables = memory.apply_to_tables(tables, hints, work)
            except Exception as e:
                warnings.append(f"Memory not applied: {type(e).__name__}: {e}")
        if llm is not None:
            llm.lessons = memory.prompt_block(skills)

        # the cleaned tables stay downloadable (CSV / Parquet) after the work folder is gone
        if not use_spark:
            try:
                _export(tables, out_html)
            except Exception as e:
                warnings.append(f"Cleaned data not exported: {type(e).__name__}: {e}")

        # 2. profile -----------------------------------------------------
        results = []
        for i, t in enumerate(tables):
            cancel.check()
            def prog(p, m, i=i):
                # profilers report 0.5..0.9; spread that window across tables
                progress(0.5 + (0.4 * i + max(0.0, p - 0.5)) / len(tables), m)
            if use_spark:
                res = spark_engine.profile_table(t, prog)
            else:
                from .profiling import profile_table
                res = profile_table(t, prog)
            for c in res["columns"]:
                c["narrative"] = describe_column(c)
            _features(res)
            memory.rename_columns(res, hints.get("rename_columns"))
            results.append(res)

        # 3. LLM ---------------------------------------------------------
        if llm is not None:
            from .llm import describe_columns, summarize_dataset
            from .llm_anomaly import review_table
            n = len(results)
            for i, res in enumerate(results):
                base = 0.9 + 0.09 * i / n
                # AI anomaly review first, so descriptions and summary can mention its findings
                if config.AI_ANOMALY_REVIEW and config.EDA_QUALITY:
                    review_table(llm, res, lambda p, m: progress(base + 0.03 * p / n, m))
                if config.AI_SUMMARY:
                    progress(base + 0.03 / n, f"AI summary for {res['name']}")
                    res["summary"] = summarize_dataset(llm, res, lang)
                if config.AI_DESCRIPTIONS and config.EDA_UNIVARIATE:
                    describe_columns(llm, res, lambda p, m: progress(base + (0.04 + 0.05 * p) / n, m), lang)
                if res.get("llm_errors"):
                    warnings.append(f"LLM errors on {res['name']}: {res['llm_errors'][0]}")

        # 4. report ------------------------------------------------------
        cancel.check()
        progress(0.99, "Rendering report")
        result = {
            "file": {"name": Path(path_s.rstrip("/")).name, "size": size, "format": fmt},
            "engine": "spark" if use_spark else "duckdb (local)",
            "tables": results, "llm": llm_info, "warnings": warnings, "lang": lang,
            "elapsed_s": time.time() - t0, "report_id": report_id,
            "features": {"descriptive": config.EDA_DESCRIPTIVE, "univariate": config.EDA_UNIVARIATE,
                         "multivariate": config.EDA_MULTIVARIATE, "quality": config.EDA_QUALITY,
                         "duplicates": config.EDA_DUPLICATES, "memory": config.MEMORY_ENABLED},
            "skills": [{"id": k["id"], "title": k.get("title"), "instruction": k.get("instruction"),
                        "hints": k.get("hints")} for k in skills],
        }
        memory.mark_used(skills)
        from .report import render
        render(result, out_html)
        out_html.with_suffix(".json").write_text(json.dumps(result, default=str, indent=1), encoding="utf-8")
        progress(1.0, "Done")
        return result
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)


def _export(tables, out_html: Path):
    """Write each table (original columns + any '(filled)' companions) as Parquet next to the report."""
    from .duck import connect, ident, lit, parquet_src
    d = out_html.with_name(out_html.stem + "_data")
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        con.execute("SET preserve_insertion_order=true")
        for i, t in enumerate(tables):
            cols = ", ".join(ident(c) for c in t.columns)
            con.execute(f"COPY (SELECT {cols} FROM {parquet_src([t.path] + list(t.extra_paths))}) "
                        f"TO {lit((d / f'table{i}.parquet').as_posix())} (FORMAT parquet, COMPRESSION zstd)")
    finally:
        con.close()


def _features(res: dict):
    """Leave out what the Settings page switched off."""
    if not config.EDA_QUALITY:
        for c in res["columns"]:
            c["flags"] = []
        res["table_flags"] = []
        res["n_warnings"] = 0
    if not config.EDA_UNIVARIATE:
        for c in res["columns"]:
            c["histogram"] = None
            c["narrative"] = []
            c["top_values"] = c.get("top_values", [])[:1]       # 'top' / 'freq' of the describe table


def _size(path: str):
    try:
        return Path(path).stat().st_size
    except OSError:
        return None
