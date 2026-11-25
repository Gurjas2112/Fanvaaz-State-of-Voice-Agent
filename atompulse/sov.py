"""Share-of-Voice metrics.

For keyword k, each result video v at rank r gets a position weight

    w(r) = 1 / log2(r + 1)          (r=1 -> 1.00, r=10 -> 0.29, r=50 -> 0.18)

the same discount search-ranking metrics (DCG) use, because people see and
click the top results far more than the 50th.

Per brand b:

    m(v,b) = 2 if b is in the title, +1 if in the description, +1 per comment naming b
    M_b = sum_v w * m(v,b)                                   mention share  = M_b / sum M
    e(v) = ln(1+views) + 2 ln(1+likes) + 3 ln(1+comments)    (log damps viral outliers)
    E_b = sum_v w * e(v) * m(v,b) / sum_b' m(v,b')            engagement share = E_b / sum E
    P_b = sum_v w * positive mentions of b in v               positive share   = P_b / sum P

    SoV_b = 0.3 * M + 0.4 * E + 0.3 * P   (weights in config; reported separately too)

Shares are fractions in [0, 1] and each sums to 1 across brands with any mention.
"""

from __future__ import annotations

import math
from collections import defaultdict

from .brands import all_brands
from .config import Settings, get_settings
from .models import BrandMetrics, KeywordReport, Mention, Video

SOURCE_WEIGHT = {"title": 2.0, "description": 1.0, "comment": 1.0}


def rank_weight(rank: int) -> float:
    if rank < 1:
        raise ValueError("rank starts at 1")
    return 1.0 / math.log2(rank + 1)


def engagement(v: Video) -> float:
    return (
        math.log1p(max(v.view_count, 0))
        + 2 * math.log1p(max(v.like_count, 0))
        + 3 * math.log1p(max(v.comment_count, 0))
    )


def _share(d: dict[str, float]) -> dict[str, float]:
    total = sum(d.values())
    return {k: (v / total if total > 0 else 0.0) for k, v in d.items()}


def compute(
    videos: list[Video], mentions: list[Mention], settings: Settings | None = None, keyword: str = "all"
) -> KeywordReport:
    s = settings or get_settings()
    wsum = s.w_mentions + s.w_engagement + s.w_positive
    wm, we, wp = s.w_mentions / wsum, s.w_engagement / wsum, s.w_positive / wsum

    # A video can rank for several keywords; each (keyword, video) is one result slot.
    slots = {(v.keyword, v.video_id): v for v in videos}
    per_slot: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    pos_slot: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    raw = defaultdict(int)
    pos = defaultdict(int)
    neg = defaultdict(int)
    seen_comment_brand: set[tuple[str, str, str]] = set()

    for m in mentions:
        key = (m.keyword, m.video_id)
        if key not in slots:
            continue
        if m.source == "comment":
            dedupe = (m.keyword, m.source_id, m.brand)
            if dedupe in seen_comment_brand:
                continue
            seen_comment_brand.add(dedupe)
        per_slot[key][m.brand] += SOURCE_WEIGHT[m.source]
        raw[m.brand] += 1
        if m.sentiment == "positive":
            pos_slot[key][m.brand] += 1
            pos[m.brand] += 1
        elif m.sentiment == "negative":
            neg[m.brand] += 1

    M: dict[str, float] = defaultdict(float)
    E: dict[str, float] = defaultdict(float)
    P: dict[str, float] = defaultdict(float)
    videos_mentioning: dict[str, int] = defaultdict(int)
    for key, counts in per_slot.items():
        v = slots[key]
        w = rank_weight(v.rank)
        total = sum(counts.values())
        e = engagement(v)
        for brand, c in counts.items():
            M[brand] += w * c
            E[brand] += w * e * c / total
            videos_mentioning[brand] += 1
        for brand, c in pos_slot[key].items():
            P[brand] += w * c

    brands = [b for b in all_brands() if raw[b] > 0]
    Ms, Es, Ps = (
        _share({b: M[b] for b in brands}),
        _share({b: E[b] for b in brands}),
        _share({b: P[b] for b in brands}),
    )
    rows = [
        BrandMetrics(
            brand=b,
            mentions=round(M[b], 4),
            mention_share=round(Ms[b], 4),
            engagement_share=round(Es[b], 4),
            positive_share=round(Ps[b], 4),
            sov=round(wm * Ms[b] + we * Es[b] + wp * Ps[b], 4),
            net_sentiment=round((pos[b] - neg[b]) / raw[b], 4) if raw[b] else 0.0,
            raw_mentions=raw[b],
            positive=pos[b],
            negative=neg[b],
            videos_mentioning=videos_mentioning[b],
        )
        for b in brands
    ]
    rows.sort(key=lambda r: r.sov, reverse=True)
    return KeywordReport(
        keyword=keyword,
        videos=len(slots),
        comments=sum(len(v.comments) for v in slots.values()),
        brands=rows,
    )


def by_keyword(
    videos: list[Video], mentions: list[Mention], settings: Settings | None = None
) -> dict[str, KeywordReport]:
    out: dict[str, KeywordReport] = {}
    keywords = sorted({v.keyword for v in videos})
    for k in keywords:
        out[k] = compute(
            [v for v in videos if v.keyword == k], [m for m in mentions if m.keyword == k], settings, k
        )
    out["all"] = compute(videos, mentions, settings, "all")
    return out
