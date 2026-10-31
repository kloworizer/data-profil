"""Local LLM narratives through Ollama. Every call is optional: on any failure
the report keeps the rule-based description."""
from __future__ import annotations

import json
import re

import httpx

from . import cancel, config
from .narrative import compact

SYSTEM = ("You are a senior data analyst writing an exploratory data analysis report. "
          "Be factual and concise. Only use the statistics given; do not invent numbers.")


class Ollama:
    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or config.OLLAMA_MODEL
        self.host = host or config.OLLAMA_HOST
        self._think_ok = True

    def models(self) -> list[str]:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []

    def available(self) -> bool:
        return self.model in self.models() or any(m.split(":")[0] == self.model for m in self.models())

    def has_vision(self) -> bool:
        """The model can read images (Ollama 'vision' capability)."""
        if not hasattr(self, "_vision"):
            try:
                r = httpx.post(f"{self.host}/api/show", json={"model": self.model}, timeout=10)
                self._vision = "vision" in (r.json().get("capabilities") or [])
            except Exception:
                self._vision = False
        return self._vision

    def chat(self, prompt: str, as_json: bool = True, temperature: float = 0.2, images: list[str] | None = None) -> str:
        """images: base64-encoded PNG/JPEG pages for vision models."""
        user = {"role": "user", "content": prompt}
        if images:
            user["images"] = images
        body = {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": SYSTEM}, user],
            "options": {"temperature": temperature, "num_ctx": 8192},
        }
        if as_json:
            body["format"] = "json"
        if self._think_ok:
            body["think"] = False   # skip hidden reasoning on thinking models: much faster
        cancel.check()
        try:
            return self._stream(body)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 400 or not self._think_ok:
                raise
            self._think_ok = False
            body.pop("think", None)
            return self._stream(body)

    def _stream(self, body: dict) -> str:
        """Streamed request: a stop request aborts the generation between tokens."""
        body = {**body, "stream": True}
        out = []
        with httpx.stream("POST", f"{self.host}/api/chat", json=body, timeout=config.LLM_TIMEOUT) as r:
            if r.status_code >= 400:
                r.read()
                r.raise_for_status()
            for line in r.iter_lines():
                cancel.check()
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(f"Ollama: {chunk['error']}")
                out.append((chunk.get("message") or {}).get("content") or "")
                if chunk.get("done"):
                    break
        return "".join(out)


def _parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}


LANG_RULE = {"en": "Write in simple English for a non-technical reader.",
             "id": "Tulis dalam Bahasa Indonesia yang sederhana untuk pembaca awam (bukan ahli teknis)."}


def describe_columns(llm: Ollama, table: dict, progress=lambda p, m: None, lang: str = "en") -> int:
    """Fill p['llm'] for up to LLM_MAX_COLUMNS columns. Returns #described."""
    cols = table["columns"][: config.LLM_MAX_COLUMNS]
    done = 0
    for i in range(0, len(cols), config.LLM_BATCH):
        batch = cols[i:i + config.LLM_BATCH]
        progress(i / max(1, len(cols)), f"LLM describing columns {i + 1}–{i + len(batch)} of {len(cols)}")
        payload = [compact(p) for p in batch]
        prompt = (
            f"Dataset '{table['name']}' has {table['n_rows']:,} rows and {table['n_cols']} columns.\n"
            f"Column profiles (JSON):\n{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            "For EACH column write 2-4 sentences: (1) what the column most likely represents in business terms, "
            "(2) a description of its distribution / typical values, (3) data quality concerns and a concrete "
            "cleaning suggestion if any. Avoid jargon. " + LANG_RULE.get(lang, LANG_RULE["en"]) +
            " Return JSON: {\"columns\": {\"<exact column name>\": \"<description>\"}}"
        )
        try:
            data = _parse_json(llm.chat(prompt))
            desc = data.get("columns", data) if isinstance(data, dict) else {}
            for p in batch:
                v = desc.get(p["name"])
                if isinstance(v, dict):
                    v = " ".join(str(x) for x in v.values())
                if isinstance(v, str) and v.strip():
                    p["llm"] = v.strip()
                    done += 1
        except Exception as e:  # keep going; rule-based text remains
            table.setdefault("llm_errors", []).append(f"{type(e).__name__}: {e}"[:200])
            if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                return done
        # small models sometimes drop columns from a batch: retry those one by one
        for p in batch:
            if p.get("llm"):
                continue
            try:
                data = _parse_json(llm.chat(
                    f"Column profile (JSON): {json.dumps(compact(p), ensure_ascii=False, default=str)}\n\n"
                    "Write 2-4 sentences: what the column likely represents, its distribution / typical values, "
                    "and data quality concerns with a cleaning suggestion if any. " + LANG_RULE.get(lang, LANG_RULE["en"]) +
                    " Return JSON: {\"description\": \"...\"}"))
                v = data.get("description") if isinstance(data, dict) else None
                if isinstance(v, str) and v.strip():
                    p["llm"] = v.strip()
                    done += 1
            except Exception as e:
                table.setdefault("llm_errors", []).append(f"{type(e).__name__}: {e}"[:200])
    return done


def summarize_dataset(llm: Ollama, table: dict, lang: str = "en") -> dict | None:
    cols = [{"name": p["name"], "type": p["inferred_type"], "semantic": p.get("semantic"),
             "missing": f"{p['missing_pct']:.1f}%", "distinct_values": p["distinct"],
             "issues": [f["msg"] for f in p["flags"] if f["level"] == "warn"][:2]}
            for p in table["columns"][:120]]
    rep = [f"{i['label']}: {i['count']}" for i in table["repairs"]["items"]][:10]
    prompt = (
        f"Dataset '{table['name']}': {table['n_rows']:,} rows, {table['n_cols']} columns, "
        f"{table['missing_cells_pct']:.1f}% missing cells, duplicates: {table['duplicates']}.\n"
        f"Structural repairs applied while reading: {rep or 'none'}.\n"
        f"Columns: {json.dumps(cols, ensure_ascii=False, default=str)}\n"
        f"Strong correlations: {json.dumps(table['correlations'][:8], default=str)}\n\n"
        "Write an executive summary: what this dataset is likely about (1-2 sentences), then 3-6 key observations "
        "about its content and data quality. Only cite numbers that appear "
        "verbatim above; 'missing' is the share of empty values per column. Avoid jargon. "
        + LANG_RULE.get(lang, LANG_RULE["en"]) + " "
        "Return JSON: {\"about\": str, \"observations\": [str]}"
    )
    try:
        data = _parse_json(llm.chat(prompt))
        if isinstance(data, dict) and data.get("about"):
            return data
    except Exception as e:
        table.setdefault("llm_errors", []).append(f"{type(e).__name__}: {e}"[:200])
    return None
