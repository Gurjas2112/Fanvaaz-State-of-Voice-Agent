"""Deterministic brand detection.

Rules first, LLM never: an LLM may judge sentiment, but it cannot add or remove a
brand mention. That keeps the SoV counts reproducible and makes "inject a fake
brand mention through a comment" impossible by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .config import BRAND_ALIASES


@dataclass(frozen=True)
class Span:
    brand: str
    start: int
    end: int
    alias: str


def _alias_pattern(alias: str) -> str:
    # Flexible whitespace inside multi-word aliases ("atom   berg").
    parts = [re.escape(p) for p in alias.split()]
    return r"\s+".join(parts)


@lru_cache(maxsize=1)
def _compiled() -> list[tuple[str, str, re.Pattern[str]]]:
    entries: list[tuple[str, str]] = [
        (brand, alias) for brand, aliases in BRAND_ALIASES.items() for alias in aliases
    ]
    # Longest alias first so "orient electric" wins over "orient".
    entries.sort(key=lambda e: len(e[1]), reverse=True)
    return [
        (brand, alias, re.compile(rf"(?<![\w]){_alias_pattern(alias)}(?![\w])", re.IGNORECASE))
        for brand, alias in entries
    ]


def find_spans(text: str) -> list[Span]:
    """All non-overlapping brand spans in reading order."""
    taken: list[tuple[int, int]] = []
    spans: list[Span] = []
    for brand, alias, pattern in _compiled():
        for m in pattern.finditer(text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue
            taken.append((s, e))
            spans.append(Span(brand, s, e, alias))
    spans.sort(key=lambda sp: sp.start)
    return spans


def brands_in(text: str) -> list[str]:
    """Distinct brands mentioned, in order of first appearance."""
    seen: list[str] = []
    for sp in find_spans(text):
        if sp.brand not in seen:
            seen.append(sp.brand)
    return seen


def all_brands() -> list[str]:
    return list(BRAND_ALIASES)
