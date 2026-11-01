"""Render the self-contained, bilingual (EN / ID) HTML report for non-technical
readers. Both languages are rendered into one file with an instant switch, so a
downloaded report works offline in either language."""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import bulma, i18n

TEMPLATES = Path(__file__).parent / "templates"
# classes the report's script adds after loading (the Bulma subset is taken from the HTML)
SCRIPT_CLASSES = ("is-active", "is-clipped", "is-loading", "is-selected", "notification", "is-link", "is-light",
                  "buttons", "button", "is-small")


def svg_bars(bins, width=420, height=110, lang="en"):
    if not bins:
        return Markup("")
    mx = max(n for _, n in bins) or 1
    n = len(bins)
    gap = 1 if n > 40 else 2
    bw = max(1.0, (width - gap * (n - 1)) / n)
    out = [f'<svg class="hist" viewBox="0 0 {width} {height + 18}" preserveAspectRatio="none" role="img">']
    for i, (label, c) in enumerate(bins):
        h = 0 if not c else max(1.5, (height - 4) * c / mx)
        x = i * (bw + gap)
        lab = i18n.VALUE_ID.get(str(label), str(label)) if lang == "id" else str(label)
        out.append(f'<rect x="{x:.2f}" y="{height - h:.2f}" width="{bw:.2f}" height="{h:.2f}" rx="1.5">'
                   f'<title>{html.escape(lab)}: {i18n.fmt_num(c, lang)}</title></rect>')
    first = html.escape(str(bins[0][0]).split(" – ")[0])
    last = html.escape(str(bins[-1][0]).split(" – ")[-1])
    if lang == "id":
        first, last = i18n.VALUE_ID.get(first, first), i18n.VALUE_ID.get(last, last)
    out.append(f'<text x="0" y="{height + 14}" class="axis">{first}</text>')
    out.append(f'<text x="{width}" y="{height + 14}" class="axis" text-anchor="end">{last}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def human_bytes(n):
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def quality(t: dict) -> tuple[int, str]:
    """Indicative 0-100 score: empty cells, column warnings, duplicates, layout repairs."""
    n_cols = max(1, t["n_cols"])
    score = 100.0
    score -= min(25, t.get("missing_cells_pct", 0) * 0.6)
    warn_cols = sum(1 for p in t["columns"] if any(f["level"] == "warn" for f in p["flags"]))
    score -= min(35, 35 * warn_cols / n_cols)
    dup = (t.get("duplicates") or {}).get("pct", 0) or 0
    score -= min(15, dup * 1.5)
    rows = max(1, t["n_rows"])
    score -= min(15, 100 * t["repairs"]["total"] / rows * 1.5)
    score = max(0, round(score))
    return score, ("good" if score >= 85 else "fair" if score >= 65 else "bad")


def issues(t: dict, lang: str) -> list[dict]:
    """Things to check, most important first: table-level, then column warnings, then info."""
    out = []
    for f in t.get("table_flags") or []:
        out.append({"level": f["level"], "code": f["code"], "col": None, "idx": None,
                    "text": i18n.render(f.get("m") or f["msg"], lang), "ex": [], "ai": False,
                    **i18n.flag_info(f["code"], lang)})
    for p in t["columns"]:
        for f in p["flags"]:
            out.append({"level": f["level"], "code": f["code"], "col": p["name"], "idx": p["index"],
                        "text": i18n.render(f.get("m") or f["msg"], lang), "ex": f.get("examples") or [],
                        "ai": f.get("ai", False), **i18n.flag_info(f["code"], lang)})
    minor = {"skew", "line_breaks", "future_dates", "imbalance", "whitespace", "null_tokens"}
    out.sort(key=lambda i: (i["level"] != "warn", i["code"] in minor, i["col"] is not None))
    return out


def render(result: dict, out: Path) -> Path:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "j2"]))
    env.filters.update(num=i18n.fmt_num, bytes=human_bytes, svg=svg_bars, tr=i18n.render)
    env.globals.update(type_label=lambda t, l: i18n.TYPE_LABEL[l].get(t, t),
                       sem_label=lambda s, l: i18n.SEMANTIC_LABEL[l].get(s, s) if s else "",
                       repair_label=i18n.repair_label, flag_info=i18n.flag_info, UI=i18n.UI)
    views = {}
    for lang in i18n.LANGS:
        views[lang] = [{"score": quality(t), "issues": issues(t, lang),
                        "warn_cols": sum(1 for p in t["columns"] if any(f["level"] == "warn" for f in p["flags"])),
                        # inverted pyramid: columns that need attention first, clean ones last
                        "order": sorted(t["columns"], key=lambda p: (
                            -sum(1 for f in p["flags"] if f["level"] == "warn"), -len(p["flags"]), p["index"]))}
                       for t in result["tables"]]
    tour = {lang: [{"sel": s, "title": a, "text": b} for s, a, b in steps] for lang, steps in i18n.REPORT_TOUR.items()}
    html_text = env.get_template("report.html.j2").render(
        r=result, views=views, langs=i18n.LANGS, default_lang=result.get("lang", "en"),
        tour_json=Markup(json.dumps(tour, ensure_ascii=False).replace("</", "<\\/")),
        ui_json=Markup(json.dumps({l: {k: i18n.UI[l][k] for k in ("next", "prev", "done", "close", "fb_sending", "fb_new", "fb_updated",
                                                                     "fb_none", "fb_rerun", "fb_rerunning", "fb_error",
                                                                     "fb_memory", "fb_send", "prev_col", "next_col", "col_pos")}
                                   for l in i18n.LANGS}, ensure_ascii=False)),
        now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    # only the Bulma rules this report uses, so the file stays self-contained and small
    html_text = html_text.replace("/*BULMA*/", bulma.subset(html_text, extra=SCRIPT_CLASSES), 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    return out
