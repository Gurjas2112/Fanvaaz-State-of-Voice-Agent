"""'Ask the data': grounded Q&A over the latest run.

`Answerer` is the facade the API, the MCP server and the tests use; the work is
done by the Ask graph (graph/ask_graph.py):

    guard -> route -> data:    recall -> plan -> agent -> stats -> cache -> retrieve -> context
                                  -> answer -> verify -> judge -> remember
                   -> app:     a fixed explanation of Fanvaaz itself
                   -> general: category knowledge from the model, labelled as not from the data
                   -> off_topic: a polite scope message

Without an LLM the answer is extractive (stats + quoted comments). With an LLM the
model sees the stats and cited comments inside <data>; its answer is rejected if
it states a number that is not in the evidence, cites a comment it was not given,
or looks like an injection, and the extractive answer is returned instead. A judge
then removes any sentence the data does not support.
"""

from __future__ import annotations

import re

from . import profile as profile_mod
from .brands import brands_in
from .llm import LLM
from .models import KeywordReport, Mention, Video
from .retrieval import Embedder, HybridRetriever, chunks_from_videos, load_chunks


def aspect_words(aspect: str | None) -> list[str]:
    if not aspect:
        return []
    return [aspect, *profile_mod.active().aspects.get(aspect, [])]


class Answerer:
    def __init__(
        self,
        videos: list[Video],
        mentions: list[Mention],
        reports: dict[str, KeywordReport],
        llm: LLM | None = None,
        *,
        store=None,
        run_id: str | None = None,
        embedder: Embedder | None = None,
    ):
        self.reports, self.mentions, self.llm = reports, mentions, llm
        self.videos = videos
        self.store, self.run_id, self.embedder = store, run_id, embedder
        self._risks: list[dict] | None = None
        self._prev: tuple[bool, KeywordReport | None] = (False, None)
        # (comment id, brand) -> sentiment: lets "what do people praise" retrieve praise.
        self.sentiment_of = {(m.source_id, m.brand): m.sentiment for m in mentions if m.source == "comment"}
        chunks, self.quarantined = chunks_from_videos(videos)
        vectors: dict[str, list[float]] = {}
        if store is not None and run_id:
            stored, vectors = load_chunks(store, run_id)
            if stored:
                chunks = stored
        self.retriever = HybridRetriever(chunks, vectors, embedder if vectors else None, store, run_id)
        self.valid_ids = {c.doc_id for c in chunks}
        from .graph.ask_graph import build

        self.graph = build(self)

    # ---- scope and stats (used by the graph and by the LLM eval harness) -------------
    def _scope(self, question: str) -> tuple[list[str], str, str | None]:
        brands = brands_in(question) or [profile_mod.active().focus_brand]
        return brands, self._keyword(question), self._aspect(question)

    def _keyword(self, question: str) -> str:
        low = question.lower()
        return next((k for k in self.reports if k != "all" and k.lower() in low), "all")

    @staticmethod
    def _aspect(question: str) -> str | None:
        low = question.lower()
        return next(
            (
                a
                for a, words in profile_mod.active().aspects.items()
                if a in low or any(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", low) for w in words)
            ),
            None,
        )

    def _evidence(self, brands: list[str], keyword: str, aspect: str | None) -> tuple[list[str], dict]:
        rep = self.reports.get(keyword) or self.reports["all"]
        lines, ev = [], {}
        for b in brands:
            row = next((r for r in rep.brands if r.brand == b), None)
            if not row:
                lines.append(f"{b} has no mentions for '{keyword}'.")
                continue
            rank = [r.brand for r in rep.brands].index(b) + 1
            lines.append(
                f"{b} on '{keyword}': Share of Voice {100 * row.sov:.1f}% (rank {rank} of {len(rep.brands)}), "
                f"mention share {100 * row.mention_share:.1f}%, positive share {100 * row.positive_share:.1f}%, "
                f"{row.positive} positive and {row.negative} negative of {row.raw_mentions} mentions."
            )
            ev |= {
                f"{b}_sov": row.sov,
                f"{b}_ms": row.mention_share,
                f"{b}_ps": row.positive_share,
                f"{b}_pos": row.positive,
                f"{b}_neg": row.negative,
                f"{b}_n": row.raw_mentions,
                f"{b}_rank": rank,
                f"{b}_of": len(rep.brands),
            }
            if aspect:
                ms = [
                    m
                    for m in self.mentions
                    if m.brand == b and aspect in m.aspects and (keyword == "all" or m.keyword == keyword)
                ]
                p = sum(m.sentiment == "positive" for m in ms)
                n = sum(m.sentiment == "negative" for m in ms)
                lines.append(f"On {aspect}, {b} has {p} positive and {n} negative mentions.")
                ev |= {f"{b}_{aspect}_pos": p, f"{b}_{aspect}_neg": n, f"{b}_{aspect}_n": len(ms)}
        return lines, ev

    # ---- data the tools read lazily ----------------------------------------------------
    def risk_flags(self) -> list[dict]:
        if self._risks is None:
            if self.store is not None and self.run_id:
                self._risks = self.store.risks(self.run_id)
            else:
                from .risk import flag_comments

                self._risks = flag_comments(self.videos, self.mentions)
        return self._risks

    def previous_reports(self) -> KeywordReport | None:
        """The 'all' report of the collection before this one (None on the first run)."""
        if not self._prev[0]:
            rep = None
            if self.store is not None and self.run_id:
                prev = self.store.previous_run(self.run_id)
                rep = self.store.report(prev.run_id, "all") if prev else None
            self._prev = (True, rep)
        return self._prev[1]

    # ---- the public calls ------------------------------------------------------------
    def _state(self, question: str, user_key: str | None, thread_id: str | None, fresh: bool) -> dict:
        return {"question": question, "user_key": user_key, "thread_id": thread_id, "fresh": fresh}

    def answer(
        self, question: str, *, user_key: str | None = None, thread_id: str | None = None, fresh: bool = False
    ) -> dict:
        """fresh=True skips the answer cache (the UI's "regenerate")."""
        state = self.graph.invoke(self._state(question, user_key, thread_id, fresh))
        return state["result"]

    def stream(
        self, question: str, *, user_key: str | None = None, thread_id: str | None = None, fresh: bool = False
    ):
        """Yield ("step", text) as each graph node finishes, then ("result", dict)."""
        sent = 0
        result = None
        for update in self.graph.stream(
            self._state(question, user_key, thread_id, fresh), stream_mode="updates"
        ):
            for node, out in update.items():
                out = out or {}
                # early nodes extend state["steps"]; verify and judge extend the result's trace
                steps = out.get("steps") or (out.get("result") or {}).get("reasoning_steps")
                if steps:
                    for s in steps[sent:]:
                        yield "step", s
                    sent = len(steps)
                if node == "respond":
                    result = out["result"]
        yield "result", result
