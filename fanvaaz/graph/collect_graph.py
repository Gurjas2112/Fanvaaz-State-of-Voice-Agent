"""The daily collection agent as a LangGraph state graph.

    collect -> extract -> [relabel] -> score -> risk -> insights -> [rewrite]
            -> evidence -> persist -> index

* collect   YouTube Data API v3 (live), or a recorded real snapshot (replay)
* extract   deterministic brand matching + rule sentiment
* relabel   LLM re-check of low-confidence sentiment (only if an LLM is allowed)
* score     Share of Voice per keyword
* risk      escalation radar
* insights  rule-built insights with their evidence numbers
* rewrite   LLM polish of the wording (only if allowed; numbers re-checked)
* evidence  citations for every insight, then the grounding/citation verifier
* persist   one append-only run in the database
* index     RAG chunks + embeddings for Ask

The two LLM nodes are conditional edges: no key, the kill switch, or a spent
budget routes around them and the run still completes on rules. State holds
plain dicts so every checkpoint is JSON-serialisable and a run can resume.
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from .. import evidence as evidence_mod
from .. import insights as insights_mod
from .. import obs, retrieval
from .. import profile as profile_mod
from .. import risk as risk_mod
from ..collector import QuotaExceeded, YouTubeCollector, YouTubeKeyError, load_snapshot
from ..config import Settings
from ..extract import extract_video, llm_relabel, source_texts
from ..llm import LLM
from ..models import KeywordReport, Mention, Video
from ..sov import by_keyword

log = logging.getLogger("fanvaaz.graph.collect")

NODES = [
    "collect",
    "extract",
    "relabel",
    "score",
    "risk",
    "insights",
    "rewrite",
    "evidence",
    "persist",
    "index",
]


class CollectState(TypedDict, total=False):
    run_id: str
    source: str  # youtube | replay
    snapshot: str | None
    keywords: list[str]
    profile: dict
    videos: list[dict]
    mentions: list[dict]
    reports: dict[str, dict]
    risks: list[dict]
    insights: list[dict]
    units: int
    info: dict
    rag: dict
    llm_used: bool
    completed: Annotated[list[str], operator.add]


def _videos(state) -> list[Video]:
    return [Video.model_validate(v) for v in state["videos"]]


def _mentions(state) -> list[Mention]:
    return [Mention.model_validate(m) for m in state["mentions"]]


def _reports(state) -> dict[str, KeywordReport]:
    return {k: KeywordReport.model_validate(r) for k, r in state["reports"].items()}


def build(settings: Settings, store, llm: LLM | None, embedder=None, progress=None):
    def node(name):
        def wrap(fn):
            def run_node(state: CollectState):
                obs.set_run(state.get("run_id"))
                if progress:
                    progress(name)
                # Each node runs under the run's own profile, whatever the process default is.
                with profile_mod.use(profile_mod.Profile.model_validate(state["profile"])):
                    with obs.span("collect", name):
                        out = fn(state)
                out = dict(out or {})
                out["completed"] = [name]
                return out

            return run_node

        return wrap

    @node("collect")
    def collect(state):
        if state["source"] == "replay":
            videos, _ = load_snapshot(state["snapshot"])
            videos = [v for v in videos if v.keyword in state["keywords"]]
            units = 0
        else:
            col = YouTubeCollector(settings, run_dir=settings.raw_dir / state["run_id"])
            videos = col.collect(state["keywords"])
            units = col.units
        if not videos:
            raise RuntimeError("collection returned no videos; check the keywords and the API key")
        return {"videos": [v.model_dump() for v in videos], "units": units}

    @node("extract")
    def extract(state):
        mentions: list[Mention] = []
        for v in _videos(state):
            mentions += extract_video(v)
        # Keep the rule confidence through the checkpoint (it is excluded from stored runs).
        return {"mentions": [{**m.model_dump(), "confidence": m.confidence} for m in mentions]}

    @node("relabel")
    def relabel(state):
        mentions = [Mention.model_validate(m) for m in state["mentions"]]
        texts = source_texts(_videos(state))
        mentions = llm_relabel(mentions, texts, llm, only_uncertain=True)
        return {"mentions": [m.model_dump() for m in mentions], "llm_used": True}

    @node("score")
    def score(state):
        reports = by_keyword(_videos(state), _mentions(state), settings)
        return {"reports": {k: r.model_dump() for k, r in reports.items()}}

    @node("risk")
    def risk(state):
        return {"risks": risk_mod.flag_comments(_videos(state), _mentions(state))}

    @node("insights")
    def insights(state):
        prof = profile_mod.active()
        ins = insights_mod.build(
            _reports(state), _videos(state), _mentions(state), focus=prof.focus_brand, risks=state["risks"]
        )
        return {"insights": ins}

    @node("rewrite")
    def rewrite(state):
        return {"insights": insights_mod.rewrite_with_llm(state["insights"], llm), "llm_used": True}

    @node("evidence")
    def evidence(state):
        prof = profile_mod.active()
        videos = _videos(state)
        ins = evidence_mod.attach(
            state["insights"], videos, _mentions(state), state["risks"], prof.focus_brand
        )
        return {"insights": evidence_mod.verify(ins, videos)}

    @node("persist")
    def persist(state):
        from datetime import UTC, datetime

        from ..models import RunInfo

        prof = profile_mod.active()
        videos = _videos(state)
        info = RunInfo(
            run_id=state["run_id"],
            source=state["source"],
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            keywords=sorted({v.keyword for v in videos}),
            videos=len(videos),
            comments=sum(len(v.comments) for v in videos),
            quota_units_used=state.get("units", 0),
            profile_id=prof.id,
            focus_brand=prof.focus_brand,
            brands=list(prof.brands),
        )
        store.save_run(info, videos, _mentions(state), _reports(state), state["insights"], state["risks"])
        return {"info": info.model_dump()}

    @node("index")
    def index(state):
        return {"rag": retrieval.index_run(store, state["run_id"], _videos(state), embedder)}

    def after_extract(state) -> str:
        return "relabel" if llm is not None else "score"

    def after_insights(state) -> str:
        return "rewrite" if llm is not None else "evidence"

    network = RetryPolicy(
        max_attempts=3,
        initial_interval=2.0,
        retry_on=lambda e: not isinstance(e, (QuotaExceeded, YouTubeKeyError)),
    )
    g = StateGraph(CollectState)
    g.add_node("collect", collect, retry_policy=network)
    g.add_node("extract", extract)
    g.add_node("relabel", relabel)
    g.add_node("score", score)
    g.add_node("risk", risk)
    g.add_node("insights", insights)
    g.add_node("rewrite", rewrite)
    g.add_node("evidence", evidence)
    g.add_node("persist", persist)
    g.add_node("index", index, retry_policy=RetryPolicy(max_attempts=2))
    g.add_edge(START, "collect")
    g.add_edge("collect", "extract")
    g.add_conditional_edges("extract", after_extract, {"relabel": "relabel", "score": "score"})
    g.add_edge("relabel", "score")
    g.add_edge("score", "risk")
    g.add_edge("risk", "insights")
    g.add_conditional_edges("insights", after_insights, {"rewrite": "rewrite", "evidence": "evidence"})
    g.add_edge("rewrite", "evidence")
    g.add_edge("evidence", "persist")
    g.add_edge("persist", "index")
    g.add_edge("index", END)
    return g


def mermaid() -> str:
    """The graph as Mermaid (for the README and /api/v1/admin/graphs)."""
    from ..config import get_settings

    class _Fake:
        def complete(self, *a, **k):
            return ""

    return build(get_settings(), None, _Fake()).compile().get_graph().draw_mermaid()
