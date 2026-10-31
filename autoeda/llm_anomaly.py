"""AI-assisted anomaly review with the local Ollama model.

Rules catch structural problems; this layer handles problems that need language
understanding:
  * values that mean the same thing written differently (Jkt / Jakarta / DKI Jakarta)
  * hidden "no data" placeholders (xxx, belum diisi, 0000)
  * values that do not belong in a column (a city inside a product column)
  * meaningful names for columns without a header (column_1 ...)
  * a second opinion on rows the rule-based repair found ambiguous

The model may only point at values that really exist; every suggestion is
validated against the data before it reaches the report, and is labelled AI.
"""
from __future__ import annotations

import difflib
import json
import re

from . import config
from .llm import Ollama, _parse_json
from .models import msg
from .profiling import NULL_TOKENS, _flag

GENERIC_NAME = re.compile(r"^column_\d+$")
MAX_REVIEW_COLUMNS = 15
MAX_AMBIGUOUS_ROWS = 12


def _norm(s: str) -> str:
    return re.sub(r"[\s._\-]+", " ", str(s).strip().lower())


def _related(a: str, b: str) -> bool:
    """Guard against wrong groupings: variants must look related to the standard
    (contained, an abbreviation, or a close spelling)."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    it = iter(long_.replace(" ", ""))
    if len(short.replace(" ", "")) >= 2 and all(ch in it for ch in short.replace(" ", "")):
        return True    # abbreviation / subsequence: jkt -> jakarta
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def review_column(llm: Ollama, table_name: str, p: dict) -> list[dict]:
    values = p.get("value_list") or []
    if len(values) < 3:
        return []
    exists = {str(v) for v, _ in values}
    prompt = (
        f"Column '{p['name']}' in dataset '{table_name}'. All distinct values with their counts (JSON):\n"
        f"{json.dumps(values, ensure_ascii=False)}\n\n"
        "Find data quality problems. Return JSON with three lists:\n"
        '{"same_meaning": [{"standard": "<best spelling>", "variants": ["<value>", "<value>"]}],\n'
        ' "placeholders": ["<value>"],\n'
        ' "misfits": [{"value": "<value>", "reason": "<short reason>"}]}\n'
        "Rules: copy values EXACTLY as listed. same_meaning = different spellings, abbreviations or typos of the "
        "SAME real-world thing (not merely similar categories). placeholders = values that mean no data / "
        "unknown / not filled. misfits = values that clearly do not belong in this column. "
        "Use empty lists when there is nothing. Do not invent values."
    )
    try:
        data = _parse_json(llm.chat(prompt))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    flags = []
    # 1. variant groups
    groups = []
    for g in data.get("same_meaning") or []:
        if not isinstance(g, dict):
            continue
        std = str(g.get("standard") or "").strip()
        vs = [str(v) for v in (g.get("variants") or []) if str(v) in exists]
        vs = list(dict.fromkeys(vs))
        if not std or len(vs) < 2 and not (len(vs) == 1 and vs[0] != std and std in exists):
            continue
        members = list(dict.fromkeys(vs + ([std] if std in exists else [])))
        if len(members) < 2 or len({_norm(m) for m in members}) < 2:
            continue      # pure case/space differences are reported by the rule-based check
        if not all(_related(m, std) for m in members):
            continue
        groups.append([" / ".join(repr(m) for m in members if m != std), std])
    if groups:
        flags.append(_flag("warn", "ai_variants", "{n} groups of values probably mean the same thing, e.g. {ex}.",
                           examples=groups[:6], n=len(groups), ex=f"{groups[0][0]} → '{groups[0][1]}'") | {"ai": True})
    # 2. placeholders
    known = set(NULL_TOKENS)
    ph = [str(v) for v in (data.get("placeholders") or []) if str(v) in exists and _norm(v) not in known]
    ph = list(dict.fromkeys(ph))[:8]
    if ph:
        flags.append(_flag("warn", "ai_placeholders", "These values look like 'no data' placeholders: {vals}.",
                           vals=", ".join(repr(v) for v in ph)) | {"ai": True})
    # 3. misfits
    mis = []
    for m in data.get("misfits") or []:
        if isinstance(m, dict) and str(m.get("value")) in exists and str(m.get("value")) not in ph:
            mis.append([str(m["value"]), str(m.get("reason") or "")[:120]])
    if mis and len(mis) <= max(3, len(values) // 3):    # a long misfit list means the model misunderstood
        flags.append(_flag("info", "ai_suspicious", "{n} values do not seem to fit this column, e.g. {ex}.",
                           examples=mis[:6], n=len(mis), ex=repr(mis[0][0])) | {"ai": True})
    return flags


def suggest_names(llm: Ollama, table: dict) -> int:
    cols = [p for p in table["columns"] if GENERIC_NAME.match(p["name"])]
    if not cols:
        return 0
    payload = {p["name"]: [str(v[0])[:40] for v in (p.get("value_list") or p.get("top_values") or [])][:8]
               or [p.get("inferred_type")] for p in cols[:40]}
    try:
        data = _parse_json(llm.chat(
            "These table columns have no header. Sample values per column (JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Suggest a short, clear name (1-3 words, snake_case) for each column based on its values. "
            'Return JSON: {"names": {"<column>": "<suggested_name>"}}'))
    except Exception:
        return 0
    names = data.get("names", data) if isinstance(data, dict) else {}
    done = 0
    for p in cols:
        n = names.get(p["name"])
        if isinstance(n, str) and re.fullmatch(r"[\w .\-/]{1,40}", n.strip()):
            p["ai_name"] = n.strip()
            p["notes"].append(msg("AI suggests the name: {name}", name=n.strip()))
            done += 1
    return done


def review_rows(llm: Ollama, table: dict) -> list[dict]:
    """Second opinion on rows where the rule-based repair had near-equal options."""
    amb = (table["repairs"].get("ambiguous") or [])[:MAX_AMBIGUOUS_ROWS]
    if not amb:
        return []
    header = [p["name"] for p in table["columns"]]
    example = {p["name"]: (str(p["top_values"][0][0]) if p["top_values"] else "") for p in table["columns"]}
    rows = [{"line": a["line"], "text": a["raw"]} for a in amb]
    try:
        data = _parse_json(llm.chat(
            f"A table has these {len(header)} columns: {json.dumps(header, ensure_ascii=False)}.\n"
            f"A typical row looks like: {json.dumps(example, ensure_ascii=False)}\n"
            f"These lines were damaged (wrong separators, values shifted or glued). For each line, split it into "
            f"exactly {len(header)} values in column order, using an empty string for a missing value. "
            f"Lines (JSON): {json.dumps(rows, ensure_ascii=False)}\n"
            'Return JSON: {"rows": [{"line": <line number>, "values": ["...", ...]}]}'))
    except Exception:
        return []
    by_line = {}
    for r in (data.get("rows") or []) if isinstance(data, dict) else []:
        if isinstance(r, dict) and isinstance(r.get("values"), list) and len(r["values"]) == len(header):
            by_line[r.get("line")] = [str(v) for v in r["values"]]
    out = []
    for a in amb:
        ai = by_line.get(a["line"])
        if ai is None:
            continue
        same = [_norm(x) for x in ai] == [_norm(x) for x in a["chosen"]]
        out.append({"line": a["line"], "raw": a["raw"], "chosen": a["chosen"], "ai": ai, "agree": same})
    return out


def review_table(llm: Ollama, table: dict, progress=lambda p, m: None) -> dict:
    """Run all AI checks on one profiled table; mutates the profile in place."""
    stats = {"columns_reviewed": 0, "findings": 0, "names": 0, "rows": 0}
    cand = [p for p in table["columns"]
            if p.get("value_list") and p["inferred_type"] in ("categorical", "text")
            and p.get("semantic") not in ("identifier", "email", "url", "uuid", "ip_address", "phone", "json")]
    cand = cand[:MAX_REVIEW_COLUMNS]
    for i, p in enumerate(cand):
        progress(i / max(1, len(cand)), f"AI checking values in '{p['name']}'")
        found = review_column(llm, table["name"], p)
        p["flags"].extend(found)
        stats["columns_reviewed"] += 1
        stats["findings"] += len(found)
    stats["names"] = suggest_names(llm, table)
    table["ai_rows"] = review_rows(llm, table)
    stats["rows"] = len(table["ai_rows"])
    table["ai_review"] = stats
    table["n_warnings"] = sum(1 for p in table["columns"] for f in p["flags"] if f["level"] == "warn")
    return stats
