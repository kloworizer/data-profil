"""Bulma CSS (vendored v1.0.4, MIT, static/BULMA_LICENSE): the whole file is served to the
app pages; a report gets only the rules its own HTML uses, so a downloaded report stays
self-contained without carrying ~680 KB of CSS."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

CSS_FILE = Path(__file__).parent / "static" / "bulma.min.css"
_CLASS = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
_ATTR = re.compile(r'class\s*=\s*"([^"]*)"')
_NESTED = ("@media", "@container", "@supports", "@layer")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LICENSE = "/*! bulma.io v1.0.4 | MIT License | github.com/jgthms/bulma */"


@lru_cache(maxsize=1)
def css() -> str:
    return CSS_FILE.read_text(encoding="utf-8")


def _rules(text: str):
    """Top-level (prelude, body) pairs; body is None for statements such as @charset."""
    i, n = 0, len(text)
    while i < n:
        start, quote = i, None
        while i < n:                          # prelude up to '{' or ';'
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch in "{;":
                break
            i += 1
        prelude = _COMMENT.sub("", text[start:i]).strip()
        if i >= n:
            if prelude:
                yield prelude, None
            return
        if text[i] == ";":
            i += 1
            if prelude:
                yield prelude, None
            continue
        depth, i, body_start, quote = 1, i + 1, i + 1, None
        while i < n and depth:                # body up to the matching '}'
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        yield prelude, text[body_start:i - 1]


def _split(selector: str) -> list[str]:
    """Selector list split at top-level commas (not inside :not(...) or [...])."""
    out, depth, cur = [], 0, []
    for ch in selector:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and not depth:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _needs(part: str) -> set[str]:
    """Classes an element must carry for the selector to match; :not(...) and [...] need none."""
    part = re.sub(r"\[[^\]]*\]", "", part)
    while ":not(" in part:
        part = re.sub(r":not\([^()]*\)", "", part)
    return set(_CLASS.findall(part))


def _filter(text: str, used: frozenset) -> str:
    out = []
    for prelude, body in _rules(text):
        if body is None:
            if not prelude.startswith("@charset"):
                out.append(prelude + ";")
        elif "prefers-color-scheme" in prelude:
            continue                           # a report always names its theme (data-theme)
        elif prelude.startswith(_NESTED):
            inner = _filter(body, used)
            if inner:
                out.append(f"{prelude}{{{inner}}}")
        elif prelude.startswith("@"):
            out.append(f"{prelude}{{{body}}}")   # @keyframes and friends: kept whole
        else:
            keep = [p for p in _split(prelude) if _needs(p) <= used]
            if keep:
                out.append(f"{','.join(keep)}{{{body}}}")
    return "".join(out)


@lru_cache(maxsize=32)
def _subset(used: frozenset) -> str:
    return LICENSE + _filter(css(), used)


def subset(html_text: str, extra=()) -> str:
    """The Bulma rules that can match the classes in `html_text` (plus `extra`, for classes
    that only scripts add). The page must set `data-theme` on <html>: the copies of the
    theme colours for the system preference are left out."""
    used = {c for attr in _ATTR.findall(html_text) for c in attr.split()}
    return _subset(frozenset(used | set(extra)))
