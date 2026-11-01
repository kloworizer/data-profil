"""Memory: users comment on a report, the comment becomes a reusable skill.

A skill is a small, editable record:
  title / instruction   what was learned, in words; the instruction is added to every
                        prompt of the local model on later runs it applies to
  keywords / formats    when it applies: words found in the file name, the first page
                        and the column names of a later file
  hints                 structured changes the rules obey (HINTS below)

Learning: the comment and the table it was made on go to the local model, which writes
the skill in general terms (for documents of that kind, not for one file) and says
whether it refines an existing skill. Without the model, common requests are parsed by
rules. Comments on the same kind of document update one skill instead of piling up
copies, so skills sharpen over time. Every skill can be edited, disabled or deleted on
the Memory page; skills apply at once.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from . import config

HINTS = {
    "header_rows": "number of header rows of the tables (1-4)",
    "join_tables": "true: tables read separately (page by page, sheet blocks) with the same columns are one table",
    "drop_rows": "list of regular expressions; rows whose first filled cell matches one are not data",
    "column_types": "{column name: text | identifier | category}; keep these columns as text / IDs / categories",
    "rename_columns": "{column name as read: better name}",
}
TYPES = ("text", "identifier", "category")
STOP = set("""the and for with from that this are was were have has into per not all any but each than
yang dan dari untuk dengan pada dalam atau oleh ini itu adalah tidak tabel table data file pdf xlsx csv
total jumlah kolom column columns row rows baris page halaman""".split())
MAX_RECALL = 6
_lock = threading.Lock()


def _dir() -> Path:
    d = config.MEMORY_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zà-ÿ][a-zà-ÿ0-9]{2,}", (text or "").lower()) if w not in STOP}


# --- store ---------------------------------------------------------------------------------------
def load() -> list[dict]:
    """Skills, with how often this server has used them."""
    p = _dir() / "skills.json"
    if not p.exists():
        return []
    try:
        skills = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    usage = _usage()
    for s in skills:
        s.update(usage.get(s["id"], {}))
    return skills


def _save(skills: list[dict]):
    """skills.json is meant to be committed and synced with git: stable order and keys, and
    no per-run counters (those stay in this server's data folder)."""
    p = _dir() / "skills.json"
    tmp = p.with_suffix(".tmp")
    rows = [{k: v for k, v in s.items() if k not in LOCAL} for s in sorted(skills, key=lambda s: s.get("created", 0))]
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


LOCAL = ("uses", "last_used")


def _usage_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "memory_usage.json"


def _usage() -> dict:
    try:
        return json.loads(_usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clear() -> dict:
    """Forget everything: all skills and the comments they came from."""
    with _lock:
        n = len(load())
        fb = _dir() / "feedback.jsonl"
        m = len(fb.read_text(encoding="utf-8").splitlines()) if fb.exists() else 0
        _save([])
        fb.unlink(missing_ok=True)
        _usage_path().unlink(missing_ok=True)
    return {"skills": n, "feedback": m}


def feedback_log(limit: int = 100) -> list[dict]:
    p = _dir() / "feedback.jsonl"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(l) for l in reversed(lines) if l.strip()]


def update_skill(skill_id: str, changes: dict) -> dict | None:
    with _lock:
        skills = load()
        s = next((s for s in skills if s["id"] == skill_id), None)
        if s is None:
            return None
        for k in ("title", "instruction", "enabled"):
            if k in changes:
                s[k] = changes[k]
        if "keywords" in changes:
            s["keywords"] = sorted({w.lower().strip() for w in changes["keywords"] if w and w.strip()})
        if "formats" in changes:
            s["formats"] = [f for f in changes["formats"] if f]
        if "hints" in changes:
            s["hints"] = clean_hints(changes["hints"])
        s["updated"] = time.time()
        s["version"] = s.get("version", 1) + 1
        _save(skills)
        return s


def delete_skill(skill_id: str) -> bool:
    with _lock:
        skills = load()
        rest = [s for s in skills if s["id"] != skill_id]
        if len(rest) == len(skills):
            return False
        _save(rest)
        return True


# --- hints ---------------------------------------------------------------------------------------
def clean_hints(h) -> dict:
    """Keep only well-formed hints; anything else is dropped."""
    out: dict = {}
    if not isinstance(h, dict):
        return out
    n = h.get("header_rows")
    if isinstance(n, (int, float, str)) and str(n).strip().isdigit() and 1 <= int(str(n).strip()) <= 4:
        out["header_rows"] = int(str(n).strip())
    if h.get("join_tables") is True or str(h.get("join_tables")).lower() == "true":
        out["join_tables"] = True
    pats = h.get("drop_rows")
    if isinstance(pats, str):
        pats = [pats]
    if isinstance(pats, list):
        ok = []
        for p in pats[:10]:
            try:
                re.compile(str(p))
                ok.append(str(p)[:200])
            except re.error:
                pass
        if ok:
            out["drop_rows"] = ok
    ct = h.get("column_types")
    if isinstance(ct, dict):
        ct = {str(k)[:120]: str(v).lower() for k, v in ct.items() if str(v).lower() in TYPES}
        if ct:
            out["column_types"] = ct
    rn = h.get("rename_columns")
    if isinstance(rn, dict):
        rn = {str(k)[:120]: str(v)[:120] for k, v in rn.items() if str(k).strip() and str(v).strip()}
        if rn:
            out["rename_columns"] = rn
    return out


def merge_hints(skills: list[dict]) -> dict:
    out: dict = {}
    for s in skills:
        for k, v in (s.get("hints") or {}).items():
            if k in ("column_types", "rename_columns"):
                out.setdefault(k, {}).update(v)
            elif k == "drop_rows":
                out.setdefault(k, [])
                out[k] += [p for p in v if p not in out[k]]
            else:
                out[k] = v
    return out


# --- recall --------------------------------------------------------------------------------------
def context_of(path: Path, fmt: str = "", columns: list[str] | None = None) -> dict:
    """What a later file looks like to the memory: its name, a little of its text, and
    (after reading) its column names."""
    text = ""
    try:
        if fmt == "pdf" or path.suffix.lower() == ".pdf":
            import pdfplumber
            with pdfplumber.open(str(path)) as pdf:
                text = " ".join((p.extract_text() or "")[:1500] for p in pdf.pages[:2])
        elif path.is_file() and path.suffix.lower() in (".csv", ".tsv", ".txt", ".json", ".xml"):
            with open(path, "rb") as f:
                text = f.read(4096).decode("utf-8", "ignore")
    except Exception:
        pass
    return {"file": path.name, "format": fmt or path.suffix.lower().lstrip("."), "text": text,
            "columns": list(columns or [])}


def recall(ctx: dict, skills: list[dict] | None = None) -> list[dict]:
    """Enabled skills whose keywords the document shows (and of a matching format)."""
    words = tokens(ctx.get("file", "")) | tokens(ctx.get("text", "")) | tokens(" ".join(ctx.get("columns") or []))
    low = " ".join([ctx.get("file", ""), ctx.get("text", ""), " ".join(ctx.get("columns") or [])]).lower()
    scored = []
    for s in skills if skills is not None else load():
        if not s.get("enabled", True):
            continue
        if s.get("formats") and ctx.get("format") and ctx["format"] not in s["formats"]:
            continue
        kws = s.get("keywords") or []
        if not kws:
            continue
        hit = sum(1 for k in kws if (k in words if " " not in k else k in low))
        need = min(len(kws), 3)
        if hit >= max(1, (need + 1) // 2):
            scored.append((hit / len(kws), hit, s))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [s for _, _, s in scored[:MAX_RECALL]]


def mark_used(skills: list[dict]):
    if not skills:
        return
    with _lock:
        usage = _usage()
        for s in skills:
            u = usage.setdefault(s["id"], {})
            u["uses"] = u.get("uses", 0) + 1
            u["last_used"] = time.time()
        _usage_path().write_text(json.dumps(usage), encoding="utf-8")


def prompt_block(skills: list[dict]) -> str:
    lines = [f"- {s['instruction']}" for s in skills if s.get("instruction")]
    if not lines:
        return ""
    return ("Lessons learned from user feedback on similar files (follow them when they apply):\n"
            + "\n".join(lines))


# --- applying hints to what was read ------------------------------------------------------------------
MAX_REWRITE_ROWS = 2_000_000      # joins / row filters rewrite the table in memory


def apply_to_tables(tables: list, hints: dict, work: Path) -> list:
    """Learned changes after reading: join tables that are one, drop rows that are not
    data, mark column types. Returns the new table list."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from .models import Table

    def read(t):
        parts = [pq.read_table(p) for p in [t.path] + list(t.extra_paths)]
        return pa.concat_tables(parts, promote_options="default") if len(parts) > 1 else parts[0]

    small = sum(t.n_rows for t in tables) <= MAX_REWRITE_ROWS
    if hints.get("join_tables") and small and len(tables) > 1:
        out = []
        for t in tables:
            prev = out[-1] if out else None
            if prev is not None and len(prev.columns) == len(t.columns) \
                    and sum(a == b for a, b in zip(prev.columns, t.columns)) >= 0.5 * len(t.columns):
                names = dict(zip(t.columns, prev.columns))          # matched by position
                a, b = read(prev), read(t)
                b = b.rename_columns([names.get(n, n) for n in b.column_names])
                text = lambda x: x.cast(pa.schema([(n, pa.string()) for n in x.column_names]))  # noqa: E731
                both = pa.concat_tables([text(a), text(b)], promote_options="default")
                path = work / f"{Path(prev.path).stem}_j.parquet"
                pq.write_table(both, path)
                cols = list(prev.columns) + [n for n in b.column_names if n not in prev.columns]
                drop = list(dict.fromkeys(list(prev.drop_columns) + [names.get(n, n) for n in t.drop_columns]))
                joined = Table(prev.name, path, cols, [], drop, both.num_rows,
                               {**prev.source, "pages": _span(prev.source, t.source)}, prev.repairs)
                joined.repairs.note("Joined with the next table ({name}): learned from user feedback.", name=t.name)
                out[-1] = joined
            else:
                out.append(t)
        tables = out
    pats = [re.compile(p, re.I) for p in hints.get("drop_rows") or []]
    if pats and small:
        for t in tables:
            data = read(t)
            first = None
            for name in [c for c in data.column_names if c not in t.drop_columns]:
                col = pc.cast(data[name], pa.string())
                first = col if first is None else pc.if_else(pc.is_null(first), col, first)
            if first is None:
                continue
            keep = pa.array([not (v is not None and any(p.search(v.strip()) for p in pats))
                             for v in first.to_pylist()])
            dropped = len(keep) - pc.sum(keep).as_py()
            if dropped:
                path = work / f"{Path(t.path).stem}_f.parquet"
                pq.write_table(data.filter(keep), path)
                t.path, t.extra_paths, t.n_rows = path, [], t.n_rows - dropped
                t.repairs.counts["note_row_skipped"] += dropped
                t.repairs.note("{n} rows dropped by a rule learned from user feedback.", n=dropped)
    types = hints.get("column_types") or {}
    for t in tables:
        low = {k.lower(): v for k, v in types.items()}
        t.type_hints = {c: low[c.lower()] for c in t.columns if c.lower() in low}
    return tables


def _span(a: dict, b: dict) -> str | None:
    pa_, pb_ = str(a.get("pages") or ""), str(b.get("pages") or "")
    if not pa_ or not pb_:
        return a.get("pages")
    return f"{pa_.split('-')[0]}-{pb_.split('-')[-1]}"


def rename_columns(result: dict, renames: dict):
    """Learned better names for columns, applied to the profile (the data keeps its names)."""
    low = {k.lower(): v for k, v in (renames or {}).items()}
    for c in result.get("columns", []):
        new = low.get(str(c["name"]).lower())
        if new and new != c["name"]:
            c["original_name"], c["name"] = c["name"], new


# --- learning ------------------------------------------------------------------------------------
LEARN_PROMPT = """A user of a data-profiling tool commented on how one table of a file was read.
Turn the comment into a reusable SKILL for files of the same kind (same publisher, same
layout, same column names), not just for this one file.

File: {file} ({format})
Table: {table} (pages / sheet: {where}); {rows} rows
Columns: {columns}
First page text: {text}

User comment: "{comment}"

Existing skills (id | title | keywords):
{existing}

Structured hints the reader obeys (use only these keys, only when the comment asks for it):
{hints}

Reply as JSON:
{{"action": "create" | "update" | "none",
  "skill_id": "<id of the existing skill to refine, for update>",
  "title": "<short name, max 8 words>",
  "instruction": "<one or two sentences, general, written as advice for reading such files>",
  "keywords": ["<3-8 lowercase words that identify such files: publisher, document type, typical column names>"],
  "formats": ["pdf" | "xlsx" | "csv" ...],
  "hints": {{}}}}
Use "update" when an existing skill covers the same kind of file and the same problem.
Use "none" if the comment is not about how the data was read or understood."""


def learn(comment: str, ctx: dict, llm=None) -> dict:
    """Store the comment and turn it into a new or refined skill.
    Returns {"action", "skill", "feedback"}."""
    comment = (comment or "").strip()[:2000]
    if not comment:
        raise ValueError("Empty comment")
    fb = {"id": uuid.uuid4().hex[:10], "time": time.time(), "comment": comment,
          "file": ctx.get("file"), "table": ctx.get("table"), "report": ctx.get("report"),
          "columns": (ctx.get("columns") or [])[:40]}
    with _lock:
        skills = load()
    rules = parse_comment(comment, ctx)
    near = _similar(skills, rules)            # the skill this comment would refine, if any
    proposal = _ask_model(comment, ctx, skills, near, llm) if llm is not None else None
    if proposal is None:
        proposal = rules
        if near is not None:                  # no model to rewrite it: add the comment as a sentence
            proposal["instruction"] = _join_sentences(near.get("instruction"), comment)
            proposal["title"] = near.get("title")
    else:
        # the parser reads explicit numbers and names reliably; the model the wording. A hint
        # the model proposes counts only when the comment talks about that thing.
        model_hints = {k: v for k, v in clean_hints(proposal.get("hints")).items() if _plausible(k, v, comment)}
        proposal["hints"] = {**model_hints, **rules["hints"]}
        words = [str(k).lower().strip() for k in proposal.get("keywords") or []]
        words = [k for k in words if re.fullmatch(r"[a-zà-ÿ0-9][a-zà-ÿ0-9 .-]{1,40}", k) and k not in STOP]
        proposal["keywords"] = list(dict.fromkeys(words + rules["keywords"]))[:12]
        if near is not None and str(proposal.get("instruction") or "").strip() == (near.get("instruction") or "").strip():
            # the model kept the old wording: the new lesson goes in as a sentence of its own
            proposal["instruction"] = _join_sentences(near.get("instruction"), comment)
    action = proposal.get("action") or "create"
    if action == "none" and rules["hints"]:
        action = "create"
    with _lock:
        skills = load()
        skill = None
        if action != "none":
            skill = next((s for s in skills if s["id"] == proposal.get("skill_id")), None) \
                or _similar(skills, proposal)
            action = "update" if skill else "create"
        if action != "none":
            if skill is None:
                skill = {"id": uuid.uuid4().hex[:10], "created": time.time(), "uses": 0, "enabled": True,
                         "version": 0, "sources": [], "hints": {}, "keywords": [], "formats": []}
                skills.append(skill)
            _apply(skill, proposal)
            skill["sources"] = (skill.get("sources") or []) + [fb["id"]]
            _save(skills)
        fb["skill"] = skill["id"] if skill else None
        fb["action"] = action
        with open(_dir() / "feedback.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(fb, ensure_ascii=False) + "\n")
    return {"action": action, "skill": skill, "feedback": fb}


def _apply(skill: dict, p: dict):
    if p.get("title"):
        skill["title"] = str(p["title"])[:120]
    instr = str(p.get("instruction") or "").strip()
    if instr:
        skill["instruction"] = instr[:1200]      # the refined wording covers the earlier lessons
    kws = {str(k).lower().strip() for k in p.get("keywords") or [] if str(k).strip()}
    skill["keywords"] = sorted(set(skill.get("keywords") or []) | {k for k in kws if k not in STOP})[:20]
    fmts = [str(f).lower() for f in p.get("formats") or [] if f]
    skill["formats"] = sorted(set(skill.get("formats") or []) | set(fmts))
    hints = merge_hints([{"hints": skill.get("hints") or {}}, {"hints": clean_hints(p.get("hints"))}])
    skill["hints"] = hints
    skill.setdefault("title", "Lesson from feedback")
    skill.setdefault("instruction", "")
    skill["updated"] = time.time()
    skill["version"] = skill.get("version", 0) + 1


def _similar(skills: list[dict], p: dict) -> dict | None:
    """The existing skill for the same kind of file: one skill per kind of document
    gathers its lessons (a newer hint replaces an older one of the same key)."""
    kws = {str(k).lower() for k in p.get("keywords") or []}
    fmts = set(p.get("formats") or [])
    best, score = None, 0.0
    for s in skills:
        sk = set(s.get("keywords") or [])
        if not sk or not kws or (fmts and s.get("formats") and not fmts & set(s["formats"])):
            continue
        overlap = len(sk & kws) / min(len(sk), len(kws))
        if overlap >= 0.5 and overlap > score:
            best, score = s, overlap
    return best


def _ask_model(comment: str, ctx: dict, skills: list[dict], near: dict | None, llm) -> dict | None:
    from .llm import _parse_json
    existing = "\n".join(f"{s['id']} | {s.get('title', '')} | {', '.join(s.get('keywords') or [])}"
                         for s in skills[-30:]) or "(none)"
    if near is not None:
        existing += (f"\n\nSkill {near['id']} is for the same kind of file. Its current instruction: "
                     f"\"{near.get('instruction', '')}\". If you update it, write one instruction that keeps "
                     f"its lessons and adds the new one.")
    prompt = LEARN_PROMPT.format(
        file=ctx.get("file", "?"), format=ctx.get("format", "?"), table=ctx.get("table", "?"),
        where=ctx.get("where", "?"), rows=ctx.get("rows", "?"),
        columns=", ".join((ctx.get("columns") or [])[:30]), text=(ctx.get("text") or "")[:600],
        comment=comment, existing=existing, hints="\n".join(f"- {k}: {v}" for k, v in HINTS.items()))
    try:
        data = _parse_json(llm.chat(prompt, temperature=0))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("action") not in ("create", "update", "none"):
        return None
    if data["action"] != "none" and not str(data.get("instruction") or "").strip():
        return None
    return data


# --- rule-based reading of a comment (no model, and to check the model) ----------------------------
def parse_comment(comment: str, ctx: dict) -> dict:
    c = comment.lower()
    cols = ctx.get("columns") or []
    hints: dict = {}
    m = re.search(r"(?:header|judul|kepala)[^.\d]{0,40}?(\d)\s*(?:rows?|baris|lines?|levels?|tingkat)", c) \
        or re.search(r"(\d)\s*(?:rows?|baris|lines?|levels?|tingkat)\s*(?:of\s+)?(?:header|judul)", c)
    if m:
        hints["header_rows"] = int(m.group(1))
    if re.search(r"(?:tables?|tabel)\s*\d+\s*(?:and|dan|&|,)\s*\d+.*?(?:one|same|satu|sama|single|merge|gabung)", c) \
            or re.search(r"(?:merge|join|gabung\w*|satukan)\s+(?:the\s+)?(?:tables?|tabel)", c) \
            or re.search(r"(?:one|satu)\s+(?:table|tabel)", c):
        hints["join_tables"] = True
    types = {}
    for col in cols:
        cl = col.lower()
        if len(cl) < 2 or cl not in c:
            continue
        after = c[c.index(cl) + len(cl):][:60]
        if re.search(r"\b(?:id|identifier|identitas|kode|code|nip|nik|nomor induk|key)\b", after):
            types[col] = "identifier"
        elif re.search(r"\b(?:category|kategori|categorical)\b", after):
            types[col] = "category"
        elif re.search(r"(?<!not )(?<!bukan )\b(?:text|teks)\b|\bnot a number\b|\bbukan angka\b", after):
            types[col] = "text"
    if types:
        hints["column_types"] = types
    m = re.search(r"(?:remove|drop|hapus|buang|ignore|abaikan)\s+(?:the\s+)?(?:rows?|baris)\s+"
                  r"(?:containing|with|starting with|berisi|dengan|yang)?\s*['\"“]([^'\"”]+)['\"”]", comment, re.I)
    whole = False                       # 'remove rows containing X': a part of the label
    if not m:
        m = re.search(r"['\"“]([^'\"”]+)['\"”][^.]{0,40}?\b(?:is a total|is not data|are not data|adalah total|"
                      r"bukan data|baris total|jangan dihitung|not a record)", comment, re.I)
        whole = True                    # "'X' is a total": the whole label
    if m:
        hints["drop_rows"] = [_general_pattern(m.group(1).strip(), ctx, whole)]
    m = re.search(r"(?:rename|ganti nama|ubah nama)\s+(?:column\s+|kolom\s+)?['\"“]?([^'\"”]+?)['\"”]?\s+"
                  r"(?:to|jadi|menjadi)\s+['\"“]?([^'\"”.]+)['\"”]?", comment, re.I)
    if m:
        hints["rename_columns"] = {m.group(1).strip(): m.group(2).strip()}
    kws = _keywords(ctx)
    return {"action": "create", "title": comment[:60], "instruction": comment[:400], "keywords": kws,
            "formats": [ctx["format"]] if ctx.get("format") else [], "hints": hints}


def _general_pattern(value: str, ctx: dict, whole: bool = False) -> str:
    """A row label quoted in a comment, as a pattern for files of the same kind: words that
    name this document's own subject (they appear in its file name or title, like the
    district in 'Kecamatan Manggelewa') match any word."""
    own = tokens(ctx.get("file", "")) | tokens((ctx.get("text") or "")[:300])
    parts = [r"\S+" if w.lower() in own and i > 0 else re.escape(w) for i, w in enumerate(value.split())]
    body = r"\s+".join(parts)
    return f"^{body}$" if whole else body


def _plausible(key: str, value, comment: str) -> bool:
    """A structured hint from the model is used only when the comment is about it."""
    c = comment.lower()
    if key == "header_rows":
        return bool(re.search(r"header|judul|kepala kolom|heading", c)) and str(value) in c
    if key == "join_tables":
        return bool(re.search(r"(?:table|tabel)[^.]*(?:one|same|satu|sama|merge|join|gabung)|"
                              r"(?:merge|join|gabung|satukan)[^.]*(?:table|tabel)", c))
    if key == "drop_rows":
        return bool(re.search(r"remove|drop|delete|hapus|buang|abaikan|ignore|not data|bukan data|total", c))
    if key == "column_types":
        return all(str(k).lower() in c for k in value)
    if key == "rename_columns":
        return bool(re.search(r"rename|ganti nama|ubah nama|call it|sebut", c))
    return False


def _join_sentences(old: str | None, new: str) -> str:
    new = new.strip()
    if not old:
        return new
    if new.lower() in old.lower():
        return old
    return f"{old.rstrip('. ')}. {new}"[:1200]


def _keywords(ctx: dict) -> list[str]:
    """Words that identify files of this kind: column names first, then the file name."""
    out: list[str] = []
    for w in list(tokens(" ".join(ctx.get("columns") or []))) + sorted(tokens(ctx.get("file", ""))):
        if w not in out and not w.isdigit():
            out.append(w)
    return out[:8]
