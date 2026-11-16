"""Step 4: pull every number out of Claude's text and match it against the metrics.

A number in the text passes when it equals some metric value at the precision it was
written (e.g. "4.3" matches 4.31 -> 4.3, "1,240" matches 1240), its absolute value
(so "a 45.2% drop" matches change_pct = -45.2), a number inside a quoted video title,
or one of a few structural constants ("last 10 uploads", "per 1,000 views", "out of 100").
"""
from __future__ import annotations

import re
from typing import Any, Iterable

NUM_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")

STRUCTURAL = {0, 1, 2, 3, 4, 5, 7, 10, 20, 30, 60, 90, 100, 1000}


def _leaves(obj: Any) -> Iterable[Any]:
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


def allowed_values(*sources: Any) -> tuple[set[float], set[str]]:
    nums: set[float] = set()
    title_nums: set[str] = set()
    for src in sources:
        for leaf in _leaves(src):
            if isinstance(leaf, bool) or leaf is None:
                continue
            if isinstance(leaf, (int, float)):
                nums.add(float(leaf))
            elif isinstance(leaf, str):
                for t in NUM_RE.findall(leaf):
                    title_nums.add(t.replace(",", "").lstrip("-"))
    return nums, title_nums


def extract(text: str) -> list[str]:
    return NUM_RE.findall(text or "")


def _matches(tok: str, nums: set[float], title_nums: set[str]) -> bool:
    clean = tok.replace(",", "")
    neg = clean.startswith("-")
    body = clean.lstrip("-")
    if body in title_nums:
        return True
    try:
        val = float(body)
    except ValueError:
        return False
    if not neg and val in STRUCTURAL:
        return True
    decimals = len(body.split(".")[1]) if "." in body else 0
    for v in nums:
        for cand in (v, abs(v)):
            if round(cand, decimals) == (-val if neg else val) or (not neg and round(abs(cand), decimals) == val):
                return True
    return False


def check_text(text: str, nums: set[float], title_nums: set[str]) -> list[str]:
    return [t for t in extract(text) if not _matches(t, nums, title_nums)]


def check_section(section: Any, nums: set[float], title_nums: set[str], skip_keys: set[str] = frozenset()) -> list[str]:
    bad: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k not in skip_keys:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            bad.extend(check_text(o, nums, title_nums))

    walk(section)
    return bad
