"""One run = the collection graph (graph/collect_graph.py), checkpointed.

`run()` starts a new run; `resume()` continues a failed one from its last
completed node (same run id, no quota spent again on finished steps).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import obs
from . import profile as profile_mod
from .config import Settings, get_settings
from .governance import audit
from .graph import checkpointer
from .graph.collect_graph import build
from .llm import LLM, get_llm
from .models import RunInfo
from .retrieval import Embedder
from .store import Store

log = logging.getLogger("fanvaaz.pipeline")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _embedder(llm: LLM | None, s: Settings, store: Store) -> Embedder | None:
    if llm is None or not hasattr(llm, "embed"):
        return None
    emb = Embedder(llm, s, store)
    return emb if emb.available() else None


def run(
    source: str = "youtube",
    keywords: list[str] | None = None,
    settings: Settings | None = None,
    store: Store | None = None,
    llm: LLM | None = None,
    use_llm: bool = True,
    snapshot: str | Path | None = None,
    actor: str = "system",
    progress=None,
    run_id: str | None = None,
    profile: profile_mod.Profile | None = None,
) -> RunInfo:
    """source="youtube" collects live; source="replay" re-scores a recorded real snapshot."""
    s = settings or get_settings()
    store = store or Store.from_settings(s)
    obs.bind(store)
    if source not in ("youtube", "replay"):
        raise ValueError("source must be 'youtube' (live) or 'replay' (a recorded snapshot)")
    if source == "replay" and not snapshot:
        raise ValueError("replay needs a snapshot file")
    if source == "youtube" and not s.youtube_api_key:
        raise RuntimeError("Set FANVAAZ_YOUTUBE_API_KEY to collect live data")
    # The profile to collect: given, else this request's (set by the API middleware), else the default.
    prof = profile or profile_mod.scoped() or profile_mod.sync_from(store)  # NoProfileError before onboarding
    llm = llm if llm is not None else (get_llm(s) if use_llm else None)
    run_id = run_id or new_run_id()
    obs.new_trace(run_id)
    audit(store, actor, "run.start", run_id, {"source": source, "keywords": keywords or prof.keywords})
    graph = build(s, store, llm, _embedder(llm, s, store), progress).compile(checkpointer=checkpointer(s))
    state = {
        "run_id": run_id,
        "source": source,
        "snapshot": str(snapshot) if snapshot else None,
        "keywords": keywords or prof.keywords,
        "profile": prof.model_dump(),
        "units": 0,
        "llm_used": False,
        "completed": [],
    }
    try:
        with profile_mod.use(prof):
            out = graph.invoke(state, {"configurable": {"thread_id": run_id}})
    except Exception as e:
        audit(store, actor, "run.failed", run_id, {"error": str(e)[:300]})
        raise
    info = RunInfo.model_validate(out["info"])
    audit(
        store,
        actor,
        "run.done",
        run_id,
        {
            "videos": info.videos,
            "comments": info.comments,
            "units": info.quota_units_used,
            "llm": out.get("llm_used"),
            "rag": out.get("rag"),
        },
    )
    log.info(
        "run %s saved: %d videos, %d comments, %d quota units, nodes=%s",
        run_id,
        info.videos,
        info.comments,
        info.quota_units_used,
        out.get("completed"),
    )
    return info


def resume(
    run_id: str, settings: Settings | None = None, store: Store | None = None, actor: str = "system"
) -> RunInfo:
    s = settings or get_settings()
    store = store or Store.from_settings(s)
    obs.bind(store)
    cfg = {"configurable": {"thread_id": run_id}}
    saver = checkpointer(s)
    llm = get_llm(s)
    graph = build(s, store, llm, _embedder(llm, s, store)).compile(checkpointer=saver)
    snap = graph.get_state(cfg)
    if not snap.values:
        raise ValueError(f"no checkpoint for run {run_id}")
    if not snap.next:
        return RunInfo.model_validate(snap.values["info"])
    prof = profile_mod.Profile.model_validate(snap.values["profile"])
    obs.new_trace(run_id)
    audit(store, actor, "run.resume", run_id, {"from": list(snap.next), "done": snap.values.get("completed")})
    try:
        with profile_mod.use(prof):
            out = graph.invoke(None, cfg)
    except Exception as e:
        audit(store, actor, "run.failed", run_id, {"error": str(e)[:300], "resumed": True})
        raise
    info = RunInfo.model_validate(out["info"])
    audit(store, actor, "run.done", run_id, {"resumed": True, "rag": out.get("rag")})
    return info


def run_all(store: Store, actor: str = "scheduler", settings: Settings | None = None) -> list[dict]:
    """Collect every saved profile, one after another (the daily job).

    One profile failing does not stop the others; each outcome is returned and audited.
    With no saved profiles, the configured default is collected once.
    """
    saved = [profile_mod.Profile.model_validate(p["profile"]) for p in store.profiles()]
    targets = saved or [profile_mod.sync_from(store)]
    results = []
    for prof in targets:
        try:
            info = run(source="youtube", settings=settings, store=store, actor=actor, profile=prof)
            results.append({"profile": prof.id, "ok": True, "run": info})
        except Exception as e:  # noqa: BLE001 - the next profile still runs
            log.exception("collection for profile %s failed", prof.id)
            results.append({"profile": prof.id, "ok": False, "error": str(e)[:300]})
    return results
