"""Risk radar: comments that need escalating, not just counting.

Adapted from the team's CRIF India hackathon project (COEP Mindspark 2023,
1st prize), which scored news articles with weighted word lists (negative +10,
lawsuit +30, harassment +50) and tagged risk categories for a Tableau dashboard.

What changed for real-world use, and why:

* Category-aware terms: a "sparking" comment is a safety escalation; a
  generic 4,472-word negative list is replaced by the brand-targeted sentiment
  engine.
* Negation-aware: "no sparking issue at all" does not fire (the original counted
  any matching token).
* Brand-attributed: the risk goes to the brand in the same clause, so
  "Samsung caught fire, switched to OnePlus" flags Samsung, not OnePlus.
* Each category counts once per comment, and comment likes raise the score
  logarithmically (a liked safety complaint reaches more buyers).
* Hinglish "fir" (= "phir", then) and the name "Sue" are deliberately not terms.
* Framing-aware: on real comments, "the thumbnail makes it look like it's on
  fire" is about the video, not the product. A safety term in a clause that
  talks about looks, thumbnails or hypotheticals is not a safety escalation.
* Every flag keeps its evidence clause, so a human can verify it in seconds.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import profile
from .brands import find_spans
from .models import Mention, Video
from .sentiment import NEGATORS, _clauses

RISK_CATEGORIES: dict[str, dict] = {
    "safety": {
        "weight": 50,
        "terms": [
            "sparking",
            "spark",
            "sparks",
            "short circuit",
            "electric shock",
            "shock",
            "current lag",
            "caught fire",
            "fire",
            "burning smell",
            "smell of burning",
            "smoke",
            "smoking",
            "melted",
            "aag",
            "jal gaya",
            "जल गया",
        ],
    },
    "legal_consumer": {
        "weight": 30,
        "terms": [
            "consumer court",
            "consumer forum",
            "legal notice",
            "lawsuit",
            "fraud",
            "scam",
            "cheated",
            "cheating",
            "refund",
            "chargeback",
            "police complaint",
            "national consumer helpline",
            "complaint filed",
            "false claims",
            "misleading",
        ],
    },
    "product_failure": {
        "weight": 20,
        "terms": [
            "stopped working",
            "not working",
            "dead",
            "burnt",
            "defective",
            "faulty",
            "error code",
            "replaced twice",
            "broke",
            "broken",
            "kharab",
            "band ho gaya",
        ],
    },
    "service_failure": {
        "weight": 15,
        "terms": [
            "no response",
            "no one came",
            "never came",
            "technician did not come",
            "ticket closed",
            "not resolved",
            "waiting since",
            "pathetic service",
            "poor service",
            "worst service",
            "no reply",
            "call not picked",
            "never picked",
        ],
    },
}

SEVERITY_HIGH = 60
SEVERITY_MEDIUM = 30


def _term_re(term: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(p) for p in term.split())
    return re.compile(r"(?<![\w])" + body + r"(?![\w])", re.IGNORECASE)


@profile.per_profile
def _compiled(p) -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    """Base terms that hold for any physical product, plus the profile's own."""
    extra = p.risk_terms
    out = {}
    for cat, spec in RISK_CATEGORIES.items():
        terms = list(dict.fromkeys([*spec["terms"], *extra.get(cat, [])]))
        out[cat] = [(t, _term_re(t)) for t in terms]
    return out


profile.on_change(_compiled.cache_clear)
_TOK = re.compile(r"[a-z']+")


@dataclass
class RiskHit:
    category: str
    term: str
    clause: str
    clause_start: int
    clause_end: int
    pos: int  # absolute offset of the term in the text


def _negated(clause: str, start: int) -> bool:
    before = _TOK.findall(clause[:start].lower())[-3:]
    return any(b in NEGATORS for b in before)


# Found on the real snapshot: safety words used about a video's framing, not the product.
_FRAMING = re.compile(
    r"\b(thumbnail|look(s|ing)? like|made to look|idea that|as if|meme|joke|clickbait|title)\b", re.IGNORECASE
)


def find_risks(text: str) -> list[RiskHit]:
    hits: list[RiskHit] = []
    for cs, ce, clause, _ in _clauses(text):
        framed = bool(_FRAMING.search(clause))
        for cat, terms in _compiled().items():
            if cat == "safety" and framed:
                continue
            for term, rx in terms:
                m = rx.search(clause)
                if m and not _negated(clause, m.start()):
                    hits.append(RiskHit(cat, term, clause.strip(), cs, ce, cs + m.start()))
                    break  # one term per category per clause is enough
    return hits


def reach_multiplier(like_count: int) -> float:
    """0 likes x1.0, 9 likes x1.5, 99 likes x2.0: liked complaints reach more buyers."""
    return 1 + math.log10(1 + max(like_count, 0)) / 2


def severity(score: int, categories: list[str]) -> str:
    if "safety" in categories or score >= SEVERITY_HIGH:
        return "high"
    if score >= SEVERITY_MEDIUM:
        return "medium"
    return "low"


def flag_comments(videos: list[Video], mentions: list[Mention]) -> list[dict]:
    """One flag per (comment, brand) with at least one risk category."""
    neg_by_comment: dict[tuple[str, str], int] = {}
    for m in mentions:
        if m.source == "comment" and m.sentiment == "negative":
            neg_by_comment[(m.source_id, m.brand)] = 1
    flags: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for v in videos:
        for c in v.comments:
            hits = find_risks(c.text)
            if not hits:
                continue
            spans = find_spans(c.text)
            if not spans:
                continue
            by_brand: dict[str, list[RiskHit]] = {}
            all_brands = {sp.brand for sp in spans}
            for h in hits:
                in_clause = [sp for sp in spans if h.clause_start <= sp.start < h.clause_end]
                if in_clause:
                    # Several brands in one clause: the risk belongs to the nearest one, preferring
                    # the brand before the term ("Samsung caught fire, switched to OnePlus").
                    nearest = min(
                        in_clause,
                        key=lambda sp: abs(h.pos - sp.end) if sp.end <= h.pos else abs(sp.start - h.pos) + 25,
                    )
                    targets = {nearest.brand}
                else:
                    # No brand in the clause: only a one-brand comment can own it.
                    targets = all_brands if len(all_brands) == 1 else set()
                for b in targets:
                    by_brand.setdefault(b, []).append(h)
            for brand, bh in by_brand.items():
                if (c.comment_id, brand) in seen:
                    continue
                seen.add((c.comment_id, brand))
                cats = sorted({h.category for h in bh})
                base = sum(RISK_CATEGORIES[k]["weight"] for k in cats) + 10 * neg_by_comment.get(
                    (c.comment_id, brand), 0
                )
                score = round(base * reach_multiplier(c.like_count))
                flags.append(
                    {
                        "brand": brand,
                        "score": score,
                        "severity": severity(score, cats),
                        "categories": cats,
                        "terms": sorted({h.term for h in bh}),
                        # every distinct clause that triggered a category, in reading order
                        "evidence": " … ".join(
                            dict.fromkeys(h.clause for h in sorted(bh, key=lambda h: h.pos))
                        ),
                        "text": c.text,
                        "comment_id": c.comment_id,
                        "video_id": v.video_id,
                        "keyword": v.keyword,
                        "like_count": c.like_count,
                        "channel_title": v.channel_title,
                        "rank": v.rank,
                    }
                )
    flags.sort(key=lambda f: f["score"], reverse=True)
    return flags


def summarize(flags: list[dict]) -> dict[str, dict]:
    """brand -> {total, high, medium, low, by_category}"""
    out: dict[str, dict] = {}
    for f in flags:
        s = out.setdefault(
            f["brand"],
            {"total": 0, "high": 0, "medium": 0, "low": 0, "by_category": dict.fromkeys(RISK_CATEGORIES, 0)},
        )
        s["total"] += 1
        s[f["severity"]] += 1
        for c in f["categories"]:
            s["by_category"][c] += 1
    return out
