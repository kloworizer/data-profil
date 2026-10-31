"""Deterministic plain-language description of each column, as translatable
messages ({"tpl", "p"}); rendered per language by i18n.render. Always produced;
the LLM description (if any) is shown next to it."""
from __future__ import annotations

from .models import msg
from .profiling import _fmt


def describe_column(p: dict) -> list[dict]:
    t = p["inferred_type"]
    out = []
    if t == "empty":
        return [msg("This column is empty.")]
    sem = p.get("semantic")
    if p["numeric"]:
        n = p["numeric"]
        if sem == "identifier":
            out.append(msg("Looks like an ID number: almost every row has its own value ({d:,} different values).",
                           d=p["distinct"]))
        else:
            tpl = ("Whole numbers from {lo} to {hi} ({d:,} different values)." if t == "integer"
                   else "Decimal numbers from {lo} to {hi} ({d:,} different values).")
            out.append(msg(tpl, lo=n["min"], hi=n["max"], d=p["distinct"]))
            out.append(msg("The middle value is {med} and the average is {mean}.", med=n["median"], mean=n["mean"]))
            s = n.get("skew")
            if s is not None:
                out.append(msg("Values are spread fairly evenly around the average." if abs(s) < 0.5 else
                               "Most values are on the low side, with a few much higher ones." if s > 0 else
                               "Most values are on the high side, with a few much lower ones."))
        if p.get("units"):
            out.append(msg("Unit: {u}.", u=", ".join(u for u, _ in p["units"])))
    elif p["datetime"]:
        d = p["datetime"]
        out.append(msg("Dates from {a} to {b} (about {days:,} days).", a=d["min"][:10], b=d["max"][:10],
                       days=d["span_days"]))
        if d["has_time"]:
            out.append(msg("Includes the time of day."))
        if d.get("weekday"):
            out.append(msg("Busiest day of the week: {wd}.", wd=max(d["weekday"], key=lambda kv: kv[1])[0]))
    elif p["boolean"]:
        b = p["boolean"]
        tot = (b["true"] + b["false"]) or 1
        out.append(msg("Yes/No column: {t:.1f}% yes, {f:.1f}% no.", t=100 * b["true"] / tot, f=100 * b["false"] / tot))
    else:
        tx = p.get("text") or {}
        sub = tx.get("subtype", "text")
        if sub == "categorical":
            out.append(msg("A category column with {d:,} different values.", d=p["distinct"]))
            if p["top_values"]:
                tops = ", ".join(f"'{v}' ({pc:.0f}%)" for v, _, pc in p["top_values"][:3])
                out.append(msg("Most common: {tops}.", tops=tops))
        elif sub in ("identifier", "code"):
            out.append(msg("Looks like a code or ID: almost every row has its own value."))
        elif sub == "free text":
            out.append(msg("Free text, about {w:.0f} words per entry on average.", w=tx.get("avg_words") or 0))
        else:
            out.append(msg("Text with {d:,} different values, {a}–{b} characters long.", d=p["distinct"],
                           a=tx.get("len_min") or 0, b=tx.get("len_max") or 0))
        if sem and sem not in ("identifier", "free text", "code"):
            out.append(msg("Contains {sem}.", sem=sem))
        pats = tx.get("patterns")
        if pats and sub != "free text" and pats[0][2] >= 60:
            out.append(msg("Most values follow the format {pat} ({pct:.0f}%).", pat=pats[0][0], pct=pats[0][2]))
    out.append(msg("{pct:.1f}% of the rows are empty.", pct=p["missing_pct"]) if p["missing"] else msg("No empty values."))
    return out


def compact(p: dict) -> dict:
    """Small JSON summary of a column for the LLM prompt."""
    c = {"name": p["name"], "type": p["inferred_type"], "semantic": p.get("semantic"),
         "missing": f"{p['missing_pct']:.1f}%", "distinct_values": p["distinct"]}
    if p["numeric"]:
        n = p["numeric"]
        c["stats"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in n.items()
                      if k in ("min", "max", "mean", "median", "std", "skew", "zeros", "negatives", "outliers")}
    if p["datetime"]:
        c["stats"] = {k: p["datetime"][k] for k in ("min", "max", "span_days", "future", "has_time")}
    if p["boolean"]:
        c["stats"] = p["boolean"]
    if p["text"]:
        c["stats"] = {"avg_len": round(p["text"].get("len_avg") or 0, 1), "subtype": p["text"].get("subtype")}
    if p["top_values"]:
        c["top_values"] = [[str(v)[:40], round(pc, 1)] for v, _, pc in p["top_values"][:6]]
    if p["flags"]:
        c["issues"] = [f["msg"] for f in p["flags"]][:5]
    if p.get("notes"):
        c["notes"] = [n["text"] if isinstance(n, dict) else n for n in p["notes"]]
    return c
