"""Shared application services for the HTTP API and the MCP server.

One store per process, one cached Answerer per run (its retriever index is
built once), and the helpers both front doors need. Keeping them here means the
API and MCP tools can never drift apart in what they return.
"""

from __future__ import annotations

import threading
from functools import lru_cache

from . import obs
from .ask import Answerer
from .config import get_settings
from .llm import get_llm
from .models import RunInfo
from .pipeline import _embedder
from .store import Store


@lru_cache
def default_store() -> Store:
    store = Store.from_settings(get_settings())
    obs.bind(store)
    return store


_answerers: dict[str, Answerer] = {}
_lock = threading.Lock()


def answerer_for(info: RunInfo, store: Store) -> Answerer:
    with _lock:
        a = _answerers.get(info.run_id)
        if a is None:
            _answerers.clear()
            s = get_settings()
            llm = get_llm(s)
            reports = {k: store.report(info.run_id, k) for k in store.keywords(info.run_id)}
            a = Answerer(
                store.videos(info.run_id),
                store.mentions(info.run_id),
                reports,
                llm,
                store=store,
                run_id=info.run_id,
                embedder=_embedder(llm, s, store),
            )
            _answerers[info.run_id] = a
        return a


def clear_answerers() -> None:
    with _lock:
        _answerers.clear()
