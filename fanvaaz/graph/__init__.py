"""LangGraph agents: the daily collection graph and the Ask graph.

The checkpointer is the harness for long runs: every node's output is saved, so a
collection that dies at node 6 (quota, a provider outage, a redeploy) resumes at
node 6 with `fanvaaz resume <run_id>` instead of spending quota again.
SQLite file locally; Postgres (the same Cloud SQL database) in production.
"""

from __future__ import annotations

import sqlite3
import threading
from functools import lru_cache

from ..config import Settings

_lock = threading.Lock()


@lru_cache(maxsize=4)
def _sqlite_saver(path: str):
    from pathlib import Path

    from langgraph.checkpoint.sqlite import SqliteSaver

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


@lru_cache(maxsize=2)
def _postgres_saver(url: str):
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    conn = psycopg.connect(url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    saver = PostgresSaver(conn)
    saver.setup()
    return saver


def checkpointer(settings: Settings):
    with _lock:
        if settings.database_url:
            return _postgres_saver(settings.database_url)
        return _sqlite_saver(str(settings.checkpoint_path))
