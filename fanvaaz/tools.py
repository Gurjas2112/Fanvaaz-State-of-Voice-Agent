"""Read-only data tools for the Ask agent.

Each tool answers one well-defined question over the latest collection and returns
`ToolResult(lines, evidence, sources)`:

* lines     plain-language facts that go into the answer's context
* evidence  every number those lines contain, as raw values; the numeric grounding
            check accepts an answer's number only if it matches one of these
* sources   comments a tool found, which become citable like retrieved comments

Arguments come from a model, so every tool validates them against the active
profile (known brands, aspects, search terms) and clamps sizes. A bad argument is
reported back as a line, never raised: the agent sees what went wrong and the
answer stays grounded.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import profile as profile_mod
from .guardrails import safe_output

if TYPE_CHECKING:  # pragma: no cover
    from .ask import Answerer

TOOL_NAMES = (
    "share_of_voice",
    "brand_sentiment",
    "compare",
    "top_aspects",
    "top_videos",
    "risks",
    "change_since_last_run",
    "search_comments",
)
MAX_CALLS = 6


@dataclass
class ToolResult:
    lines: list[str] = field(default_factory=list)
    evidence: dict[str, float] = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)


class DataTools:
    def __init__(self, answerer: Answerer):
        self.a = answerer

    # ---- argument hygiene ----------------------------------------------------------
    @staticmethod
    def _brand(name) -> str | None:
        if not isinstance(name, str):
            return None
        brands = profile_mod.active().brands
        return next((b for b in brands if b.lower() == name.strip().lower()), None)

    @staticmethod
    def _aspect(name) -> str | None:
        if not isinstance(name, str):
            return None
        return next((a for a in profile_mod.active().aspects if a.lower() == name.strip().lower()), None)

    def _keyword(self, name) -> str:
        if not isinstance(name, str):
            return "all"
        return next((k for k in self.a.reports if k.lower() == name.strip().lower()), "all")

    def call(self, tool: str, args: dict) -> ToolResult:
        if tool not in TOOL_NAMES:
            return ToolResult([f"(unknown tool '{tool}' ignored)"])
        args = args if isinstance(args, dict) else {}
        try:
            return getattr(self, tool)(**{k: v for k, v in args.items() if isinstance(k, str)})
        except TypeError:
            return ToolResult([f"(tool {tool} got unusable arguments; ignored)"])

    # ---- tools ----------------------------------------------------------------------
    def share_of_voice(self, keyword="all", **_) -> ToolResult:
        kw = self._keyword(keyword)
        rep = self.a.reports.get(kw) or self.a.reports["all"]
        out = ToolResult()
        ranked = sorted(rep.brands, key=lambda r: r.sov, reverse=True)
        parts = []
        for i, r in enumerate(ranked, 1):
            parts.append(f"{i}. {r.brand} {100 * r.sov:.1f}%")
            out.evidence[f"sov_{kw}_{r.brand}"] = r.sov
            out.evidence[f"rank_{kw}_{r.brand}"] = i
        out.evidence[f"brands_{kw}"] = len(ranked)
        out.lines.append(f"Share of voice on '{kw}' ({len(ranked)} brands): " + ", ".join(parts) + ".")
        return out

    def brand_sentiment(self, brand=None, keyword="all", aspect=None, **_) -> ToolResult:
        b = self._brand(brand)
        if b is None:
            return ToolResult([f"(brand_sentiment: '{brand}' is not a tracked brand)"])
        lines, ev = self.a._evidence([b], self._keyword(keyword), self._aspect(aspect))
        return ToolResult(lines, {k: float(v) for k, v in ev.items()})

    def compare(self, brands=None, aspect=None, keyword="all", **_) -> ToolResult:
        names = [self._brand(x) for x in (brands or [])][:4] if isinstance(brands, list) else []
        names = [n for n in dict.fromkeys(names) if n]
        if len(names) < 2:
            return ToolResult(["(compare needs at least two tracked brands)"])
        lines, ev = self.a._evidence(names, self._keyword(keyword), self._aspect(aspect))
        return ToolResult(lines, {k: float(v) for k, v in ev.items()})

    def top_aspects(self, brand=None, sentiment="negative", **_) -> ToolResult:
        b = self._brand(brand)
        if b is None:
            return ToolResult([f"(top_aspects: '{brand}' is not a tracked brand)"])
        want = sentiment if sentiment in ("positive", "negative", "neutral") else "negative"
        counts = Counter(
            a for m in self.a.mentions if m.brand == b and m.sentiment == want for a in m.aspects
        )
        if not counts:
            return ToolResult([f"No {want} mentions of {b} name a tracked aspect."])
        top = counts.most_common(5)
        out = ToolResult()
        out.lines.append(
            f"Aspects in {want} mentions of {b}: " + ", ".join(f"{a} ({n})" for a, n in top) + "."
        )
        out.evidence |= {f"{b}_{want}_{a}": n for a, n in top}
        return out

    def top_videos(self, brand=None, n=3, **_) -> ToolResult:
        b = self._brand(brand) if brand else None
        try:
            n = max(1, min(int(n), 5))
        except (TypeError, ValueError):
            n = 3
        videos = {v.video_id: v for v in self.a.videos}
        if b:
            per = Counter(m.video_id for m in self.a.mentions if m.brand == b)
            ranked = [(videos[vid], c) for vid, c in per.most_common() if vid in videos][:n]
            if not ranked:
                return ToolResult([f"No video mentions {b}."])
            head = f"Videos mentioning {b} most"
        else:
            ranked = [(v, None) for v in sorted(videos.values(), key=lambda v: v.view_count, reverse=True)][
                :n
            ]
            head = "Most-viewed videos"
        out = ToolResult()
        items = []
        for i, (v, c) in enumerate(ranked, 1):
            title = safe_output(v.title)[:90]
            items.append(
                f'{i}. "{title}" by {safe_output(v.channel_title)[:40]} ({v.view_count} views'
                + (f", {c} mentions of {b}" if c is not None else "")
                + ")"
            )
            out.evidence[f"views_{v.video_id}"] = v.view_count
            if c is not None:
                out.evidence[f"mentions_{b}_{v.video_id}"] = c
        out.lines.append(head + ": " + "; ".join(items) + ".")
        return out

    def risks(self, brand=None, **_) -> ToolResult:
        b = self._brand(brand) if brand else None
        flags = self.a.risk_flags()
        if b:
            flags = [f for f in flags if f["brand"] == b]
        who = b or "all brands"
        if not flags:
            return ToolResult([f"No escalation risks are flagged for {who}."], {f"risks_{who}": 0})
        sev = Counter(f["severity"] for f in flags)
        cats = Counter(c for f in flags for c in f.get("categories", []))
        out = ToolResult()
        out.lines.append(
            f"{len(flags)} escalation risk comment(s) for {who}: "
            + ", ".join(f"{n} {s}" for s, n in sev.most_common())
            + "; categories: "
            + ", ".join(f"{c.replace('_', ' ')} ({n})" for c, n in cats.most_common(4))
            + "."
        )
        out.evidence[f"risks_{who}"] = len(flags)
        out.evidence |= {f"risk_sev_{s}": n for s, n in sev.items()}
        out.evidence |= {f"risk_cat_{c}": n for c, n in cats.items()}
        return out

    def change_since_last_run(self, brand=None, **_) -> ToolResult:
        prev = self.a.previous_reports()
        if prev is None:
            return ToolResult(["There is no earlier collection to compare with yet (this is the first run)."])
        now = {r.brand: r for r in self.a.reports["all"].brands}
        before = {r.brand: r for r in prev.brands}
        names = [self._brand(brand)] if brand and self._brand(brand) else list(now)
        out = ToolResult()
        parts = []
        for b in names:
            if b not in now or b not in before:
                continue
            d = now[b].sov - before[b].sov
            parts.append(
                f"{b} {100 * before[b].sov:.1f}% -> {100 * now[b].sov:.1f}% ({'+' if d >= 0 else ''}{100 * d:.1f} pts)"
            )
            out.evidence |= {f"prev_sov_{b}": before[b].sov, f"sov_{b}": now[b].sov, f"delta_{b}": d}
        out.lines.append(
            "Share of voice change since the previous collection: " + ("; ".join(parts) or "no overlap") + "."
        )
        return out

    def search_comments(self, query=None, brand=None, sentiment=None, **_) -> ToolResult:
        if not isinstance(query, str) or not query.strip():
            return ToolResult(["(search_comments needs a query)"])
        b = self._brand(brand) if brand else None
        want = sentiment if sentiment in ("positive", "negative") else None
        hits = self.a.retriever.search(
            query[:200], k=4, must_brand=b, want_sentiment=want, sentiment_of=self.a.sentiment_of
        )
        sources = [
            {
                "comment_id": h.chunk.doc_id,
                "video_id": h.chunk.video_id,
                "keyword": h.chunk.keyword,
                "text": safe_output(h.chunk.text),
                "score": round(h.score, 4),
                "lexical_rank": h.lexical_rank,
                "vector_rank": h.vector_rank,
                "features": h.features,
                "for_brand": b,
            }
            for h in hits
        ]
        return ToolResult([f"search_comments('{query[:60]}') found {len(sources)} comment(s)."], {}, sources)
