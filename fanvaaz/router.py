"""Question routing: which kind of answer does this question need?

    data       what viewers say, how brands compare, rank, trend; risks; what to do next
    app        how Fanvaaz works and what its terms mean (answered from fixed help text)
    general    knowledge about the product category itself (the model, labelled as such)
    off_topic  everything else (a polite scope message)

Rules decide first: they are free, instant and testable. Only a question the rules
cannot place (no brand, no data word, an "explain X" shape, or off the category's
vocabulary) goes to the LLM router, and only when a model is allowed. Without a
model, an unplaced on-topic question is answered from the data (the safe default)
and an off-topic one is declined, exactly as before routing existed.
"""

from __future__ import annotations

import re

from . import profile as profile_mod
from .brands import brands_in

DATA_WORDS = {
    "viewer", "viewers", "people", "users", "buyers", "customers", "audience", "comment", "comments",
    "say", "says", "saying", "said", "think", "thinks", "feel", "opinion", "opinions", "perceive", "perceived",
    "share", "voice", "sov", "mention", "mentions", "mentioned", "rank", "ranking", "ranks", "leads", "leader",
    "lead", "leading", "top", "most", "least", "compare", "comparison", "vs", "versus", "better", "worse",
    "complain", "complains", "complaint", "complaints", "praise", "praises", "like", "likes", "love", "hate",
    "sentiment", "positive", "negative", "video", "videos", "channel", "channels", "youtube", "trend",
    "trending", "change", "changed", "risk", "risks", "escalation", "recommendation",
    "recommendations", "focus", "strategy", "campaign", "content", "insight", "insights", "popular",
    "weak", "weakest", "strong", "strongest", "engagement", "buzz", "talk", "talking", "discuss",
    "discussed", "summarise", "summarize", "summary", "brand", "brands", "competitor", "competitors",
    "rival", "rivals", "keyword", "keywords", "issues", "problems", "reply", "replies",
}  # fmt: skip

_APP = re.compile(
    r"\b(share of voice|sov|risk radar|reply inbox|evidence pack|citations?|fanvaaz|this (app|tool|dashboard|"
    r"site))\b.*\b(mean|means|meaning|work|works|calculated|computed|measured|defined)\b"
    r"|\b(what|how)\b.*\b(is|does)\b.*\b(share of voice|sov|risk radar|reply inbox|evidence pack)\b"
    r"|^\s*(help|what can you do|how do i use)",
    re.IGNORECASE,
)
_EXPLAIN = re.compile(
    r"^\s*(what('s| is| are)|explain|define|meaning of|difference between|how (does|do)|why (is|are|does))\b",
    re.IGNORECASE,
)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def by_rules(question: str, on_topic: bool) -> tuple[str | None, str]:
    """(route, reason); route None means "ask the LLM router if one is allowed"."""
    named = brands_in(question)
    if named:
        return "data", f"names {', '.join(named[:3])}"
    if _APP.search(question):
        return "app", "asks how Fanvaaz or its metrics work"
    words = _words(question)
    data_hits = sorted(words & DATA_WORDS)
    aspects = [a for a in profile_mod.active().aspects if a in question.lower()]
    if _EXPLAIN.search(question) and not data_hits:
        return None, "an explain-style question with no data word"
    if data_hits or aspects:
        return "data", f"data words {', '.join((data_hits + aspects)[:3])}"
    if on_topic:
        return None, "on the category's vocabulary but no data word"
    return None, "outside the category's vocabulary"


def fallback(on_topic: bool) -> str:
    """The route when the rules are unsure and no LLM may decide."""
    return "data" if on_topic else "off_topic"


def help_answer(question: str) -> str:
    """Fixed, accurate help text: answers about the app never depend on a model."""
    p = profile_mod.active()
    low = question.lower()
    if re.search(r"calculat|comput|measur|formula|weight", low) or (
        ("share of voice" in low or "sov" in _words(low))
        and not re.search(r"risk|inbox|citation|evidence", low)
    ):
        return (
            "Share of voice is how much of the YouTube conversation each brand holds. For every search term, "
            "Fanvaaz reads the top 50 videos; a result's weight falls with its position (1 / log2(rank + 1)). "
            "It combines three shares: mentions (titles count double), engagement (views, likes and comments, "
            "log-damped) and positive mentions, weighted 0.3 / 0.4 / 0.3. Each share is reported separately too."
        )
    if "risk" in low or "inbox" in low or "escalat" in low:
        return (
            "The risk radar flags comments that could escalate: safety, product failure, service, refund or "
            "legal language aimed at a tracked brand. Each flag has a score (category weight, negativity and "
            "the comment's likes) and a severity; the reply inbox lists them so someone can reply, and records "
            "who handled each one."
        )
    if "citation" in low or "evidence" in low or "source" in low:
        return (
            "Every insight and answer cites the YouTube comments it relies on (c1, c2 …) and links to them. "
            "Numbers must match the computed data or the answer is rejected, and a second check removes any "
            "sentence the data does not support. The evidence pack is the full JSON behind a run."
        )
    return (
        f"Fanvaaz tracks what YouTube viewers say about {p.category} brands ({', '.join(list(p.brands)[:5])}…) "
        f"for {p.focus_brand}. Ask about share of voice, how brands compare on an aspect, what people praise or "
        "complain about, which videos matter, open risks or what to do next. Answers quote and cite real "
        "comments and only use numbers from the data."
    )


def scope_message() -> str:
    p = profile_mod.active()
    return (
        f"That is outside what I can help with here. I answer questions about how YouTube viewers discuss "
        f"{p.category} brands such as {p.focus_brand}, and about {p.category} in general."
    )
