"""FastAPI web interface: upload (streamed, any size) or server-side path,
background job queue, live progress, HTML report download.

Run: uvicorn autoeda.web:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__, cancel, config
from .cancel import CancelToken, Cancelled
from .llm import Ollama

config.ensure_dirs()
app = FastAPI(title="AutoEDA", version=__version__)
_pool = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_JOBS)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
INDEX = (Path(__file__).parent / "templates" / "index.html")


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "upload"


ACTIVE = ("queued", "running", "cancelling")
_tokens: dict[str, CancelToken] = {}     # job id -> cancel token of its current run


def _save(job: dict):
    (config.REPORT_DIR / f"{job['id']}.job.json").write_text(json.dumps(job, default=str), encoding="utf-8")


def _load_jobs():
    for f in sorted(config.REPORT_DIR.glob("*.job.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            if j.get("status") in ACTIVE:
                j.update(status="failed", message="Server restarted while the job was running.")
            _jobs[j["id"]] = j
        except Exception:
            pass


_load_jobs()


def _update(job_id: str, run: int | None = None, **kw) -> bool:
    """Update a job; with `run`, only while that run is still the job's current one
    (a stopped or restarted run must not overwrite the new run's state)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None or (run is not None and job.get("run", 1) != run):
            return False
        job.update(kw)
        _save(job)
        return True


def _input_ok(job: dict) -> bool:
    src = job.get("input")
    return bool(src) and ("://" in src or Path(src).exists())


def _drop_input(job: dict):
    """Delete an uploaded file (and its folder); server-side input files are never touched."""
    src = job.get("input")
    if not src or not job.get("cleanup") or "://" in src:
        return
    p = Path(src)
    if p.parent.parent == config.UPLOAD_DIR:
        shutil.rmtree(p.parent, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def _public(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k != "input"}
    out["can_restart"] = job.get("status") != "cancelling" and _input_ok(job)
    return out


def _run_job(job_id: str, run: int, token: CancelToken):
    from .pipeline import run as run_pipeline

    if token.cancelled:
        return    # stopped (or restarted) while waiting in the queue
    job = _jobs.get(job_id)
    if job is None or not _update(job_id, run, status="running", started=time.time(), message="Starting"):
        return
    opts, path = job["options"], job["input"]
    last = [0.0]

    def progress(p, msg):
        token.check()
        now = time.time()
        if now - last[0] > 0.5 or p >= 1:
            last[0] = now
            _update(job_id, run, progress=round(p, 3), message=msg)

    ok = False
    try:
        out = config.REPORT_DIR / f"{job_id}.html"
        with cancel.use(token):
            res = run_pipeline(path, out, job_id=f"{job_id}_{run}", engine=opts["engine"], use_llm=opts["use_llm"],
                               model=opts["model"], max_rows=opts["max_rows"], lang=opts["lang"], progress=progress)
        summary = [{"name": t["name"], "rows": t["n_rows"], "cols": t["n_cols"], "warnings": t["n_warnings"],
                    "repairs": t["repairs"]["total"]} for t in res["tables"]]
        ok = _update(job_id, run, status="done", progress=1.0, message="Done", finished=time.time(),
                     elapsed_s=res["elapsed_s"], tables=summary, warnings=res["warnings"])
    except Cancelled:
        _update(job_id, run, status="cancelled", message="Stopped by the user.", finished=time.time())
    except Exception as e:
        traceback.print_exc()
        _update(job_id, run, status="failed", message=f"{type(e).__name__}: {e}", finished=time.time())
    finally:
        with _lock:
            if _tokens.get(job_id) is token:
                _tokens.pop(job_id, None)
        if ok:
            _drop_input(_jobs.get(job_id, {}))   # failed / stopped jobs keep their input for a restart


def _start(job_id: str):
    """Queue a (new) run of a job; a run still going is told to stop."""
    token = CancelToken()
    with _lock:
        job = _jobs[job_id]
        old = _tokens.get(job_id)
        run = job.get("run", 0) + 1
        _tokens[job_id] = token
        for k in ("started", "finished", "elapsed_s", "tables", "warnings"):
            job.pop(k, None)
        job.update(run=run, status="queued", progress=0.0, message="Queued", queued=time.time())
        _save(job)
    if old is not None:
        old.cancel()
    for ext in (".html", ".json"):
        (config.REPORT_DIR / f"{job_id}{ext}").unlink(missing_ok=True)
    _pool.submit(_run_job, job_id, run, token)


def _submit(path: str, filename: str, size, opts: dict, cleanup: bool) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "file": filename, "size": size, "status": "queued", "progress": 0.0,
           "message": "Queued", "created": time.time(), "options": opts, "input": path, "cleanup": cleanup, "run": 0}
    with _lock:
        _jobs[job_id] = job
    _start(job_id)
    return _public(job)


def _opts(engine, use_llm, model, max_rows, lang="en") -> dict:
    engine = engine or config.ENGINE
    if engine not in ("local", "spark"):
        raise HTTPException(400, "engine must be 'local' or 'spark'")
    return {"engine": engine, "use_llm": bool(use_llm), "model": model or config.OLLAMA_MODEL,
            "max_rows": max(0, int(max_rows or 0)), "lang": lang if lang in ("en", "id") else "en"}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX.read_text(encoding="utf-8")


@app.put("/api/upload")
async def upload(request: Request, filename: str, engine: str = "", use_llm: bool = True, model: str = "",
                 max_rows: int = 0, lang: str = "en"):
    """Raw streaming upload (request body = file bytes). Written to disk in
    chunks, so file size is limited only by disk space."""
    opts = _opts(engine, use_llm, model, max_rows, lang)
    name = _safe_name(filename)
    dest_dir = config.UPLOAD_DIR / uuid.uuid4().hex[:12]
    dest_dir.mkdir(parents=True)
    dest = dest_dir / name
    size = 0
    with open(dest, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "Empty upload")
    return _submit(str(dest), name, size, opts, cleanup=True)


class PathJob(BaseModel):
    path: str
    engine: str = ""
    use_llm: bool = True
    model: str = ""
    max_rows: int = 0
    lang: str = "en"


@app.post("/api/path")
def from_path(req: PathJob):
    """Profile a file already on the server (under AUTOEDA_INPUT_DIR) or, with
    the Spark engine, a cluster URI such as hdfs://... or s3a://..."""
    opts = _opts(req.engine, req.use_llm, req.model, req.max_rows, req.lang)
    if "://" in req.path:
        if opts["engine"] != "spark":
            raise HTTPException(400, "Remote URIs require the Spark engine.")
        return _submit(req.path, req.path.rstrip("/").split("/")[-1], None, opts, cleanup=False)
    p = (config.INPUT_DIR / req.path).resolve() if not Path(req.path).is_absolute() else Path(req.path).resolve()
    if not p.is_relative_to(config.INPUT_DIR):
        raise HTTPException(403, f"Path must be inside the input folder {config.INPUT_DIR}")
    if not p.exists():
        raise HTTPException(404, "File not found")
    return _submit(str(p), p.name, p.stat().st_size if p.is_file() else None, opts, cleanup=False)


@app.get("/api/input-files")
def input_files():
    if not config.INPUT_DIR.exists():
        return {"dir": str(config.INPUT_DIR), "files": []}
    files = [{"path": str(f.relative_to(config.INPUT_DIR)).replace("\\", "/"), "size": f.stat().st_size}
             for f in sorted(config.INPUT_DIR.rglob("*")) if f.is_file()][:500]
    return {"dir": str(config.INPUT_DIR), "files": files}


@app.get("/api/jobs")
def jobs():
    with _lock:
        return [_public(j) for j in sorted(_jobs.values(), key=lambda j: -j["created"])[:100]]


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Unknown job")
    return _public(j)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Stop a queued or running job. Running work stops at its next checkpoint
    (DuckDB queries, Spark jobs and AI requests are interrupted)."""
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Unknown job")
    if j["status"] not in ("queued", "running"):
        raise HTTPException(409, "Job is not running")
    with _lock:
        token = _tokens.get(job_id)
    if j["status"] == "queued":
        _update(job_id, status="cancelled", message="Stopped by the user.", finished=time.time())
    else:
        _update(job_id, status="cancelling", message="Stopping")
    if token is not None:
        token.cancel()
    return _public(j)


@app.post("/api/jobs/{job_id}/restart")
def restart_job(job_id: str):
    """Run a job again from its original input (stops the current run first)."""
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Unknown job")
    if not _input_ok(j):
        raise HTTPException(410, "The original file is no longer available; please upload it again.")
    _start(job_id)
    return _public(j)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Unknown job")
    if j["status"] in ACTIVE:
        raise HTTPException(409, "Job is still running; stop it first")
    for ext in (".html", ".json", ".job.json"):
        (config.REPORT_DIR / f"{job_id}{ext}").unlink(missing_ok=True)
    with _lock:
        _jobs.pop(job_id, None)
    _drop_input(j)
    return {"deleted": job_id}


@app.get("/reports/{job_id}")
def report(job_id: str, download: bool = False):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404)
    f = config.REPORT_DIR / f"{job_id}.html"
    if not f.exists():
        raise HTTPException(404, "Report not ready")
    name = Path(_jobs.get(job_id, {}).get("file", job_id)).stem + "_eda.html"
    return FileResponse(f, media_type="text/html",
                        filename=name if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/reports/{job_id}/json")
def report_json(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404)
    f = config.REPORT_DIR / f"{job_id}.json"
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="application/json")


@app.get("/api/config")
def get_config():
    from . import spark_engine
    llm = Ollama()
    return {"engine": config.ENGINE, "spark_available": spark_engine.available(),
            "ollama_host": config.OLLAMA_HOST, "default_model": config.OLLAMA_MODEL, "models": llm.models(),
            "input_dir": str(config.INPUT_DIR), "memory_limit": config.DUCKDB_MEMORY_LIMIT,
            "threads": config.DUCKDB_THREADS}


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "version": __version__})
