"""FastAPI web interface: upload (streamed, any size) or server-side path,
background job queue, live progress, HTML report download.

Run: uvicorn autoeda.web:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from . import __version__, auth, build_info, cancel, config
from .cancel import CancelToken, Cancelled
from .llm import Ollama

config.ensure_dirs()
from . import settings  # noqa: E402

settings.apply()          # saved settings, before the job pool takes its size
app = FastAPI(title="AutoEDA", version=__version__)
_pool = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_JOBS)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
HERE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
_pages = Environment(loader=FileSystemLoader(str(HERE / "templates")), autoescape=select_autoescape(["html"]))

if config.AUTH_ENABLED:
    _pw = auth.bootstrap()
    if _pw:
        print(f"AutoEDA: created the administrator '{config.ADMIN_USER}' with the password {_pw}\n"
              "         Sign in and change it on the Users page.", file=sys.stderr, flush=True)


# --- sign-in -----------------------------------------------------------------------------------
def _client(request: Request) -> str:
    return request.client.host if request.client else ""


@app.middleware("http")
async def _signed_in(request: Request, call_next):
    """Every page and API call needs a signed-in user (session cookie, or HTTP Basic for scripts).
    State-changing calls from another site's page are refused (the cookie is SameSite=Lax too)."""
    if not config.AUTH_ENABLED:
        request.state.user = auth.LOCAL
        return await call_next(request)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != "null":
        hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
        if urlsplit(origin).netloc not in hosts:
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    user = auth.from_token(request.cookies.get(auth.COOKIE))
    basic = request.headers.get("authorization", "")
    if user is None and basic.lower().startswith("basic "):
        try:
            user = await run_in_threadpool(auth.from_basic, basic, _client(request))
        except PermissionError as e:
            return JSONResponse({"detail": str(e)}, status_code=429)
    request.state.user = auth.public(user) if user else None
    path = request.url.path
    if user is None and not (path in ("/login", "/health") or path.startswith("/static/")):
        if path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"detail": "Please sign in"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="AutoEDA"'} if basic else None)
        nxt = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
    return await call_next(request)


def _user(request: Request) -> dict:
    return request.state.user


def _admin(request: Request) -> dict:
    u = _user(request)
    if u["role"] != "admin":
        raise HTTPException(403, "Only an administrator can do this")
    return u


def _visible(job: dict, user: dict) -> bool:
    """Users see their own jobs; administrators see every job (also those from before sign-in)."""
    return user["role"] == "admin" or job.get("owner") == user["name"]


def _job(job_id: str, request: Request) -> dict:
    j = _jobs.get(job_id)
    if not j or not _visible(j, _user(request)):
        raise HTTPException(404, "Unknown job")
    return j


def _page(name: str, request: Request, **ctx) -> HTMLResponse:
    return HTMLResponse(_pages.get_template(name).render(
        user=_user(request), auth=config.AUTH_ENABLED, version=__version__, page=name.split(".")[0], **ctx))


def _session(res, request: Request, u: dict):
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    res.set_cookie(auth.COOKIE, auth.issue(u), max_age=config.SESSION_HOURS * 3600, httponly=True,
                   samesite="lax", secure=https, path="/")
    return res


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
    # an uploaded copy the app keeps (and can delete to free space, keeping the report)
    out["stored"] = bool(job.get("cleanup")) and _input_ok(job) and "://" not in (job.get("input") or "")
    return out


def _needs_staging(path: str, engine: str) -> bool:
    """An HDFS file is copied into the app unless Spark reads it on the cluster itself."""
    if not path.startswith(("hdfs://", "webhdfs://")):
        return False
    from . import spark_engine
    return engine != "spark" or not spark_engine.handles(path)


def _run_job(job_id: str, run: int, token: CancelToken):
    from .pipeline import run as run_pipeline

    if token.cancelled:
        return    # stopped (or restarted) while waiting in the queue
    job = _jobs.get(job_id)
    if job is None or not _update(job_id, run, status="running", started=time.time(), message="Starting"):
        return
    opts, path = job["options"], job["input"]
    last = [0.0]
    staged = None

    def progress(p, msg):
        token.check()
        now = time.time()
        if now - last[0] > 0.5 or p >= 1:
            last[0] = now
            _update(job_id, run, progress=round(p, 3), message=msg)

    try:
        out = config.REPORT_DIR / f"{job_id}.html"
        with cancel.use(token):
            if _needs_staging(path, opts["engine"]):
                from . import hdfs
                staged = config.WORK_DIR / "hdfs" / f"{job_id}_{run}"
                path = str(hdfs.stage(path, staged, progress))
            res = run_pipeline(path, out, job_id=f"{job_id}_{run}", engine=opts["engine"], use_llm=opts["use_llm"],
                               model=opts["model"], max_rows=opts["max_rows"], lang=opts["lang"], progress=progress,
                               report_id=job_id)
        summary = [{"name": t["name"], "rows": t["n_rows"], "cols": t["n_cols"], "warnings": t["n_warnings"],
                    "repairs": t["repairs"]["total"]} for t in res["tables"]]
        _update(job_id, run, status="done", progress=1.0, message="Done", finished=time.time(),
                     elapsed_s=res["elapsed_s"], tables=summary, warnings=res["warnings"])
    except Cancelled:
        _update(job_id, run, status="cancelled", message="Stopped by the user.", finished=time.time())
    except Exception as e:
        traceback.print_exc()
        _update(job_id, run, status="failed", message=f"{type(e).__name__}: {e}", finished=time.time())
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)
        with _lock:
            if _tokens.get(job_id) is token:
                _tokens.pop(job_id, None)
        # the input is kept until the report is deleted (any job can be run again), unless the
        # Settings page says to free the space at once
        if not config.KEEP_UPLOADS and _jobs.get(job_id, {}).get("status") == "done":
            _drop_input(_jobs.get(job_id, {}))


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


def _submit(path: str, filename: str, size, opts: dict, cleanup: bool, owner: str) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "file": filename, "size": size, "status": "queued", "progress": 0.0,
           "message": "Queued", "created": time.time(), "options": opts, "input": path, "cleanup": cleanup, "run": 0,
           "owner": owner}
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
def index(request: Request):
    return _page("index.html", request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not config.AUTH_ENABLED or _user(request):
        return RedirectResponse("/", status_code=303)
    return _page("login.html", request)


class Login(BaseModel):
    username: str
    password: str


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


@app.post("/login")
def login(request: Request, body: Login, next: str = "/"):
    try:
        u = auth.authenticate(body.username.strip(), body.password, _client(request))
    except PermissionError as e:
        raise HTTPException(429, str(e))
    if u is None:
        raise HTTPException(401, "Wrong user name or password")
    auth.touch(u["name"])
    return _session(JSONResponse({"user": auth.public(u), "next": _safe_next(next)}), request, u)


@app.post("/logout")
def logout():
    res = JSONResponse({"ok": True})
    res.delete_cookie(auth.COOKIE, path="/")
    return res


@app.get("/api/me")
def me(request: Request):
    return {"user": _user(request), "auth": config.AUTH_ENABLED}


class PasswordChange(BaseModel):
    old: str
    new: str


@app.put("/api/me/password")
def change_password(request: Request, body: PasswordChange):
    """Change your own password; your other sessions are signed out, this one stays."""
    u = _user(request)
    if not config.AUTH_ENABLED:
        raise HTTPException(409, "Sign-in is switched off on this server")
    try:
        if auth.authenticate(u["name"], body.old, _client(request)) is None:
            raise HTTPException(403, "The current password is wrong")
        auth.update(u["name"], password=body.new)
    except PermissionError as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _session(JSONResponse({"ok": True}), request, auth.load()[u["name"]])


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    return _page("users.html", request)


@app.get("/api/users")
def users_list(request: Request):
    _admin(request)
    return {"users": [auth.public(u) for u in auth.load().values()], "min_password": auth.MIN_PASSWORD}


class NewUser(BaseModel):
    name: str
    password: str
    role: str = "user"


class UserEdit(BaseModel):
    password: str | None = None
    role: str | None = None


@app.post("/api/users")
def users_add(request: Request, body: NewUser):
    _admin(request)
    try:
        return auth.create(body.name.strip(), body.password, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/users/{name}")
def users_edit(request: Request, name: str, body: UserEdit):
    _admin(request)
    try:
        return auth.update(name, password=body.password, role=body.role)
    except KeyError:
        raise HTTPException(404, "Unknown user")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/users/{name}")
def users_delete(request: Request, name: str):
    if _admin(request)["name"] == name:
        raise HTTPException(400, "You cannot delete yourself")
    try:
        auth.delete(name)
    except KeyError:
        raise HTTPException(404, "Unknown user")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"deleted": name}


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
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(400, "Empty upload")
    return _submit(str(dest), name, size, opts, cleanup=True, owner=_user(request)["name"])


class PathJob(BaseModel):
    path: str
    engine: str = ""
    use_llm: bool = True
    model: str = ""
    max_rows: int = 0
    lang: str = "en"


@app.post("/api/path")
def from_path(req: PathJob, request: Request):
    """Profile a file already on the server (under AUTOEDA_INPUT_DIR) or, with
    the Spark engine, a cluster URI such as hdfs://... or s3a://..."""
    opts = _opts(req.engine, req.use_llm, req.model, req.max_rows, req.lang)
    owner = _user(request)["name"]
    if "://" in req.path:
        if opts["engine"] != "spark" and not req.path.startswith("hdfs://"):
            raise HTTPException(400, "Remote URIs other than hdfs:// require the Spark engine.")
        from . import hdfs
        if _needs_staging(req.path, opts["engine"]) and not hdfs.enabled():
            raise HTTPException(400, "This HDFS file has to be copied into the app first: set the WebHDFS "
                                     "address in Settings, or use the Spark engine (CSV / Parquet / ORC / JSON).")
        return _submit(req.path, req.path.rstrip("/").split("/")[-1], None, opts, cleanup=False, owner=owner)
    p = (config.INPUT_DIR / req.path).resolve() if not Path(req.path).is_absolute() else Path(req.path).resolve()
    if not p.is_relative_to(config.INPUT_DIR):
        raise HTTPException(403, f"Path must be inside the input folder {config.INPUT_DIR}")
    if not p.exists():
        raise HTTPException(404, "File not found")
    return _submit(str(p), p.name, p.stat().st_size if p.is_file() else None, opts, cleanup=False, owner=owner)


@app.get("/api/hdfs")
def hdfs_list(path: str = "/"):
    """Folders and files on the host's HDFS (WebHDFS), to pick one for analysis."""
    from . import hdfs
    if not hdfs.enabled():
        raise HTTPException(404, "HDFS is not configured (WebHDFS address in Settings)")
    try:
        return {"path": hdfs.path_of(path) or "/", "entries": hdfs.listdir(hdfs.path_of(path) or "/")}
    except Exception as e:
        raise HTTPException(502, f"HDFS: {type(e).__name__}: {e}"[:300])


@app.get("/api/spark")
def spark_status():
    """The configured Spark cluster and its resources (workers, cores, memory, running apps)."""
    from . import spark_engine
    return spark_engine.status()


@app.get("/api/input-files")
def input_files():
    if not config.INPUT_DIR.exists():
        return {"dir": str(config.INPUT_DIR), "files": []}
    files = [{"path": str(f.relative_to(config.INPUT_DIR)).replace("\\", "/"), "size": f.stat().st_size}
             for f in sorted(config.INPUT_DIR.rglob("*")) if f.is_file()][:500]
    return {"dir": str(config.INPUT_DIR), "files": files}


@app.get("/api/jobs")
def jobs(request: Request):
    user = _user(request)
    with _lock:
        mine = [j for j in _jobs.values() if _visible(j, user)]
        return [_public(j) for j in sorted(mine, key=lambda j: -j["created"])[:100]]


@app.get("/api/jobs/{job_id}")
def job(job_id: str, request: Request):
    return _public(_job(job_id, request))


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    """Stop a queued or running job. Running work stops at its next checkpoint
    (DuckDB queries, Spark jobs and AI requests are interrupted)."""
    j = _job(job_id, request)
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
def restart_job(job_id: str, request: Request):
    """Run a job again from its original input (stops the current run first)."""
    j = _job(job_id, request)
    if not _input_ok(j):
        raise HTTPException(410, "The original file is no longer available; please upload it again.")
    _start(job_id)
    return _public(j)


@app.post("/api/jobs/{job_id}/drop-file")
def drop_file(job_id: str, request: Request):
    """Delete the uploaded file to free space; the report stays (it cannot be run again)."""
    j = _job(job_id, request)
    if j["status"] in ACTIVE:
        raise HTTPException(409, "Job is still running; stop it first")
    if not j.get("cleanup"):
        raise HTTPException(400, "This file is on the server's input folder, not an upload; it is never deleted")
    _drop_input(j)
    _update(job_id, file_deleted=True)
    return _public(j)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    j = _job(job_id, request)
    if j["status"] in ACTIVE:
        raise HTTPException(409, "Job is still running; stop it first")
    for ext in (".html", ".json", ".job.json"):
        (config.REPORT_DIR / f"{job_id}{ext}").unlink(missing_ok=True)
    with _lock:
        _jobs.pop(job_id, None)
    _drop_input(j)
    return {"deleted": job_id}


class Feedback(BaseModel):
    table: int = 0
    comment: str


@app.post("/api/jobs/{job_id}/feedback")
def feedback(job_id: str, fb: Feedback, request: Request):
    """A comment on one table of a report: stored, and turned into a new or refined skill
    that applies to later runs on files of that kind."""
    from . import memory

    if not config.MEMORY_ENABLED:
        raise HTTPException(409, "Learning from comments is switched off in Settings")
    j = _job(job_id, request)
    rep = config.REPORT_DIR / f"{job_id}.json"
    if not rep.exists():
        raise HTTPException(404, "Unknown report")
    if not fb.comment.strip():
        raise HTTPException(400, "Empty comment")
    res = json.loads(rep.read_text(encoding="utf-8"))
    tables = res.get("tables") or []
    if not 0 <= fb.table < len(tables):
        raise HTTPException(400, "Unknown table")
    t = tables[fb.table]
    src = j.get("input")
    ctx = memory.context_of(Path(src), (res.get("file") or {}).get("format", "")) if src and "://" not in src and _input_ok(j)         else {"file": j.get("file"), "format": (res.get("file") or {}).get("format", ""), "text": ""}
    ctx.update(table=t.get("name"), report=job_id, rows=t.get("n_rows"),
               where=(t.get("source") or {}).get("pages") or (t.get("source") or {}).get("sheet") or "",
               columns=[c.get("original_name") or c["name"] for c in t.get("columns", [])])
    llm = Ollama(j["options"].get("model")) if j["options"].get("use_llm") else None
    if llm is not None and not llm.available():
        llm = None
    out = memory.learn(fb.comment, ctx, llm)
    return {"action": out["action"], "skill": out["skill"], "can_rerun": _input_ok(j)}


@app.get("/api/memory")
def memory_list():
    from . import memory
    return {"skills": memory.load(), "feedback": memory.feedback_log(), "hint_keys": memory.HINTS}


class SkillEdit(BaseModel):
    title: str | None = None
    instruction: str | None = None
    keywords: list[str] | None = None
    formats: list[str] | None = None
    hints: dict | None = None
    enabled: bool | None = None


@app.delete("/api/memory")
def memory_clear(request: Request):
    """Clear the memory: every skill and comment is forgotten."""
    from . import memory
    _admin(request)
    return {"cleared": memory.clear()}


@app.put("/api/memory/skills/{skill_id}")
def memory_edit(skill_id: str, edit: SkillEdit, request: Request):
    from . import memory
    _admin(request)
    s = memory.update_skill(skill_id, {k: v for k, v in edit.model_dump().items() if v is not None})
    if s is None:
        raise HTTPException(404, "Unknown skill")
    return s


@app.delete("/api/memory/skills/{skill_id}")
def memory_delete(skill_id: str, request: Request):
    from . import memory
    _admin(request)
    if not memory.delete_skill(skill_id):
        raise HTTPException(404, "Unknown skill")
    return {"deleted": skill_id}


@app.get("/api/settings")
def settings_get(lang: str = "en"):
    return {"settings": settings.describe("id" if lang == "id" else "en")}


@app.put("/api/settings")
def settings_put(changes: dict, request: Request):
    _admin(request)
    try:
        settings.save(changes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"settings": settings.describe()}


@app.delete("/api/settings")
def settings_reset(request: Request):
    _admin(request)
    settings.reset()
    return {"settings": settings.describe()}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return _page("settings.html", request)


@app.get("/memory", response_class=HTMLResponse)
def memory_page(request: Request):
    return _page("memory.html", request)


@app.get("/files", response_class=HTMLResponse)
def files_page(request: Request):
    return _page("files.html", request)


def _kind(job: dict) -> str:
    src = job.get("input") or ""
    return "remote" if "://" in src else "upload" if job.get("cleanup") else "server"


def _size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def _orphans() -> list[dict]:
    """Upload folders no job refers to (e.g. left by a crash): space only an administrator can free."""
    if not config.UPLOAD_DIR.exists():
        return []
    with _lock:
        used = {Path(j["input"]).parent.name for j in _jobs.values() if j.get("cleanup") and j.get("input")}
    out = []
    for d in sorted(config.UPLOAD_DIR.iterdir()):
        if d.is_dir() and d.name not in used:
            out.append({"id": d.name, "file": ", ".join(f.name for f in d.iterdir()) or "–", "size": _size(d),
                        "modified": d.stat().st_mtime})
    return out


@app.get("/api/files")
def files_list(request: Request):
    """Uploaded files and reports of the jobs this user sees, with the space they take."""
    user = _user(request)
    with _lock:
        mine = [dict(j) for j in _jobs.values() if _visible(j, user)]
    rows = []
    for j in mine:
        kind = _kind(j)
        stored = kind == "upload" and _input_ok(j)
        report_bytes = sum(_size(config.REPORT_DIR / f"{j['id']}{ext}") for ext in (".html", ".json"))
        rows.append({"id": j["id"], "file": j.get("file"), "kind": kind, "stored": stored,
                     "size": j.get("size"), "disk": _size(Path(j["input"])) if stored else 0,
                     "report_bytes": report_bytes, "has_report": (config.REPORT_DIR / f"{j['id']}.html").exists(),
                     "created": j.get("created"), "status": j.get("status"), "owner": j.get("owner"),
                     "file_deleted": bool(j.get("file_deleted")), "active": j.get("status") in ACTIVE})
    try:
        du = shutil.disk_usage(config.DATA_DIR)
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = None
    out = {"files": rows, "disk": disk, "admin": user["role"] == "admin",
           "usage": {"uploads": sum(r["disk"] for r in rows), "reports": sum(r["report_bytes"] for r in rows),
                     "stored": sum(1 for r in rows if r["stored"])}}
    if user["role"] == "admin":
        out["orphans"] = _orphans()
        out["usage"]["orphans"] = sum(o["size"] for o in out["orphans"])
    return out


@app.delete("/api/files/orphans/{folder}")
def orphan_delete(folder: str, request: Request):
    _admin(request)
    if not re.fullmatch(r"[0-9a-f]{12}", folder) or folder not in {o["id"] for o in _orphans()}:
        raise HTTPException(404, "Unknown folder")
    shutil.rmtree(config.UPLOAD_DIR / folder, ignore_errors=True)
    return {"deleted": folder}


@app.get("/api/jobs/{job_id}/file")
def job_file(job_id: str, request: Request):
    """Download the uploaded file of a job (files on the server's input folder are not served)."""
    j = _job(job_id, request)
    if _kind(j) != "upload" or not _input_ok(j):
        raise HTTPException(404, "The file is not stored")
    return FileResponse(j["input"], filename=j.get("file") or Path(j["input"]).name,
                        content_disposition_type="attachment")


@app.get("/reports/{job_id}")
def report(job_id: str, request: Request, download: bool = False):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404)
    j = _job(job_id, request)
    f = config.REPORT_DIR / f"{job_id}.html"
    if not f.exists():
        raise HTTPException(404, "Report not ready")
    name = Path(j.get("file", job_id)).stem + "_eda.html"
    return FileResponse(f, media_type="text/html",
                        filename=name if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/reports/{job_id}/json")
def report_json(job_id: str, request: Request):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404)
    _job(job_id, request)
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
            "threads": config.DUCKDB_THREADS, "ai_default_on": config.AI_DEFAULT_ON,
            "memory_enabled": config.MEMORY_ENABLED, "keep_uploads": config.KEEP_UPLOADS,
            "spark_master": config.SPARK_MASTER, "hdfs": bool(config.HDFS_URL), "hdfs_root": config.HDFS_ROOT}


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "version": __version__, "build": build_info(),
                         "settings": settings.sources()})
