"""Turn videos + comments into brand mentions with sentiment.

Brands always come from the deterministic matcher. Sentiment comes from the rule
engine, and the LLM re-checks only the mentions the rules were unsure about
(cost: confident rule labels never reach the model). LLM answers go through the
gateway (ai.py) and are validated: the reply must match SentimentBatchOut, a
brand the matcher did not find is dropped, and any failure keeps the rule labels.
"""

from __future__ import annotations

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from . import ai, prompts
from . import profile as profile_mod
from .brands import brands_in
from .llm import LLM
from .models import Mention, Sentiment, Video
from .schemas import SentimentBatchOut
from .sentiment import aspects_of, score_text

log = logging.getLogger("fanvaaz.extract")

SENTIMENTS: set[str] = {"positive", "neutral", "negative"}

# Rule labels with |score| at or above this are trusted as they are; the rest go to the LLM.
CONFIDENT = 1.5


def _rule_mentions(video: Video, source: str, source_id: str, text: str) -> list[Mention]:
    return [
        Mention(
            video_id=video.video_id,
            keyword=video.keyword,
            source=source,
            source_id=source_id,
            brand=brand,
            sentiment=bs.sentiment,
            aspects=bs.aspects,
            confidence=abs(bs.score),
        )
        for brand, bs in score_text(text).items()
    ]


def extract_video(video: Video) -> list[Mention]:
    mentions = _rule_mentions(video, "title", video.video_id, video.title)
    mentions += _rule_mentions(video, "description", video.video_id, video.description[:1500])
    for c in video.comments:
        mentions += _rule_mentions(video, "comment", c.comment_id, c.text)
    return mentions


def llm_relabel(
    mentions: list[Mention],
    texts: dict[str, str],
    llm: LLM,
    batch_size: int = 25,
    only_uncertain: bool = False,
    workers: int = 8,
) -> list[Mention]:
    """Replace rule sentiment with LLM sentiment where the LLM gives a valid answer."""
    by_src: dict[str, list[Mention]] = {}
    for m in mentions:
        by_src.setdefault(m.source_id + "|" + m.source, []).append(m)
    keys = [k for k, ms in by_src.items() if not only_uncertain or any(m.confidence < CONFIDENT for m in ms)]
    prompt = prompts.get("sentiment", category=profile_mod.active().category)
    chunks = [keys[i : i + batch_size] for i in range(0, len(keys), batch_size)]

    def ask_batch(chunk: list[str]):
        payload = [
            {"id": k, "text": texts.get(k, "")[:600], "brands": sorted({m.brand for m in by_src[k]})}
            for k in chunk
        ]
        return ai.call(
            llm,
            prompt,
            json.dumps({"items": payload}, ensure_ascii=False),
            task="sentiment",
            node="collect.relabel",
            schema=SentimentBatchOut,
        )

    # A live run has 100+ batches at ~25 s each: one after another that outlasts Cloud Run's
    # request timeout, so batches run in parallel. Each worker gets a copy of the context
    # (run id, trace) so the cost ledger still attributes every call to this run; the budget
    # check runs per call, so a breach can overshoot by at most `workers` calls.
    workers = max(1, min(workers, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda c: contextvars.copy_context().run(ask_batch, c), chunks))

    for n, (chunk, res) in enumerate(zip(chunks, results, strict=True)):
        if not res.ok or res.parsed is None:
            log.warning("LLM sentiment batch %d kept rule labels (%s)", n, res.outcome)
            continue
        items = {it.id: {lb.brand: lb.sentiment for lb in it.labels} for it in res.parsed.items}
        for k in chunk:
            labels = items.get(k, {})
            for m in by_src[k]:
                label = labels.get(m.brand)  # a brand the matcher did not find is never added
                if label in SENTIMENTS:
                    m.sentiment = label  # type: ignore[assignment]
    return mentions


def extract_all(videos: list[Video], llm: LLM | None = None) -> list[Mention]:
    mentions: list[Mention] = []
    texts: dict[str, str] = {}
    for v in videos:
        mentions += extract_video(v)
        texts[v.video_id + "|title"] = v.title
        texts[v.video_id + "|description"] = v.description
        for c in v.comments:
            texts[c.comment_id + "|comment"] = c.text
    if llm is not None and mentions:
        mentions = llm_relabel(mentions, texts, llm, only_uncertain=True)
    return mentions


def source_texts(videos: list[Video]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for v in videos:
        texts[v.video_id + "|title"] = v.title
        texts[v.video_id + "|description"] = v.description
        for c in v.comments:
            texts[c.comment_id + "|comment"] = c.text
    return texts


def label_text(text: str) -> dict[str, Sentiment]:
    """Convenience for tests and the golden set: brand -> sentiment."""
    return {b: bs.sentiment for b, bs in score_text(text).items()}


__all__ = ["extract_all", "extract_video", "label_text", "brands_in", "aspects_of", "llm_relabel"]
