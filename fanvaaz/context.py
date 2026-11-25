"""Context engineering: what the model sees, in what order, within a token budget.

Order matters twice: for quality (the model weighs early, structured facts most)
and for cost (an unchanging prefix lets the provider cache it). The context is:

  1. stats      the computed numbers for the question's scope (always kept)
  2. memory     a one-line summary of the conversation so far (if any)
  3. comments   retrieved evidence, best first, each with a citation id (c1, c2...)
                and truncated to a fixed length; dropped from the tail when the
                budget runs out

The builder reports how much of the budget it used and what it dropped, which the
Ask response exposes and the tests assert on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import tokens
from .guardrails import safe_output

QUOTE_CHARS = 300


@dataclass
class Context:
    data: str
    citations: dict[str, dict]  # citation id -> source
    report: dict = field(default_factory=dict)


def build(
    stats: list[str],
    sources: list[dict],
    memory_summary: str = "",
    budget_tokens: int = 2500,
) -> Context:
    stats_tokens = sum(tokens.estimate(s) for s in stats)
    mem_tokens = tokens.estimate(memory_summary)
    remaining = max(budget_tokens - stats_tokens - mem_tokens - 40, 0)
    comments, citations, dropped, used = [], {}, 0, 0
    seen: set[str] = set()
    for s in sources:
        text = safe_output(" ".join(s["text"].split()))[:QUOTE_CHARS]
        key = text.lower()[:120]
        if key in seen:
            dropped += 1
            continue
        cid = f"c{len(citations) + 1}"
        entry = {"id": cid, "text": text}
        n = tokens.estimate(json.dumps(entry, ensure_ascii=False))
        if used + n > remaining:
            dropped += 1
            continue
        seen.add(key)
        used += n
        comments.append(entry)
        citations[cid] = {**s, "text": text, "citation": cid}
    payload = {"stats": stats, "comments": comments}
    if memory_summary:
        payload["conversation"] = memory_summary
    data = json.dumps(payload, ensure_ascii=False)
    total = tokens.estimate(data)
    return Context(
        data=data,
        citations=citations,
        report={
            "budget_tokens": budget_tokens,
            "used_tokens": total,
            "utilisation": round(total / budget_tokens, 3) if budget_tokens else None,
            "stats_lines": len(stats),
            "comments_kept": len(comments),
            "comments_dropped": dropped,
        },
    )
