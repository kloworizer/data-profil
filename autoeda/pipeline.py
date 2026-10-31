"""End-to-end run: ingest -> profile -> (LLM) -> HTML report."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import cancel, config
from .narrative import describe_column


def run(path: str | Path, out_html: Path, *, job_id: str = "cli", engine: str | None = None,
        use_llm: bool = True, model: str | None = None, max_rows: int = 0, lang: str = "en",
        progress=lambda p, m: None, keep_work: bool = False) -> dict:
    t0 = time.time()
    config.ensure_dirs()
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

        # 1. ingest ------------------------------------------------------
        progress(0.02, "Reading file")
        if use_spark:
            tables = spark_engine.ingest(path_s, job_id, progress, max_rows=max_rows)
            fmt = tables[0].source.get("format", "?")
            size = _size(path_s)
        else:
            from .ingest import ingest
            p = Path(path_s)
            tables, fmt = ingest(p, work, progress, max_rows=max_rows, llm=llm)
            size = p.stat().st_size
        if not tables:
            raise ValueError("No tabular data found in the file.")

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
            results.append(res)

        # 3. LLM ---------------------------------------------------------
        if llm is not None:
            from .llm import describe_columns, summarize_dataset
            from .llm_anomaly import review_table
            n = len(results)
            for i, res in enumerate(results):
                base = 0.9 + 0.09 * i / n
                # AI anomaly review first, so descriptions and summary can mention its findings
                review_table(llm, res, lambda p, m: progress(base + 0.03 * p / n, m))
                progress(base + 0.03 / n, f"AI summary for {res['name']}")
                res["summary"] = summarize_dataset(llm, res, lang)
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
            "elapsed_s": time.time() - t0,
        }
        from .report import render
        render(result, out_html)
        out_html.with_suffix(".json").write_text(json.dumps(result, default=str, indent=1), encoding="utf-8")
        progress(1.0, "Done")
        return result
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)


def _size(path: str):
    try:
        return Path(path).stat().st_size
    except OSError:
        return None
