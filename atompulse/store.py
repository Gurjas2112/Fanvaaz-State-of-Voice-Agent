"""SQLite persistence (one file, zero setup).

Runs are append-only: every collection is a new run, the API serves the latest
one, and older runs stay for comparison. Swap for Postgres by reimplementing this
module; nothing else touches SQL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .models import KeywordReport, Mention, RunInfo, Video

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, source TEXT NOT NULL, created_at TEXT NOT NULL,
  keywords TEXT NOT NULL, videos INTEGER NOT NULL, comments INTEGER NOT NULL,
  quota_units_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS videos (
  run_id TEXT NOT NULL, keyword TEXT NOT NULL, video_id TEXT NOT NULL, rank INTEGER NOT NULL,
  body TEXT NOT NULL, PRIMARY KEY (run_id, keyword, video_id)
);
CREATE TABLE IF NOT EXISTS mentions (
  run_id TEXT NOT NULL, keyword TEXT NOT NULL, brand TEXT NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentions_run ON mentions(run_id, keyword);
CREATE TABLE IF NOT EXISTS reports (
  run_id TEXT NOT NULL, keyword TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (run_id, keyword)
);
CREATE TABLE IF NOT EXISTS insights (
  run_id TEXT PRIMARY KEY, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risks (
  run_id TEXT NOT NULL, brand TEXT NOT NULL, severity TEXT NOT NULL, score INTEGER NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risks_run ON risks(run_id, brand, severity);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_run(
        self,
        info: RunInfo,
        videos: list[Video],
        mentions: list[Mention],
        reports: dict[str, KeywordReport],
        insights: list[dict],
        risks: list[dict] | None = None,
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                (
                    info.run_id,
                    info.source,
                    info.created_at,
                    json.dumps(info.keywords),
                    info.videos,
                    info.comments,
                    info.quota_units_used,
                ),
            )
            c.executemany(
                "INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?)",
                [(info.run_id, v.keyword, v.video_id, v.rank, v.model_dump_json()) for v in videos],
            )
            c.executemany(
                "INSERT INTO mentions VALUES (?,?,?,?)",
                [(info.run_id, m.keyword, m.brand, m.model_dump_json()) for m in mentions],
            )
            c.executemany(
                "INSERT OR REPLACE INTO reports VALUES (?,?,?)",
                [(info.run_id, k, r.model_dump_json()) for k, r in reports.items()],
            )
            c.execute("INSERT OR REPLACE INTO insights VALUES (?,?)", (info.run_id, json.dumps(insights)))
            c.executemany(
                "INSERT INTO risks VALUES (?,?,?,?,?)",
                [
                    (info.run_id, r["brand"], r["severity"], r["score"], json.dumps(r, ensure_ascii=False))
                    for r in (risks or [])
                ],
            )

    def latest_run(self) -> RunInfo | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        return RunInfo(
            run_id=row[0],
            source=row[1],
            created_at=row[2],
            keywords=json.loads(row[3]),
            videos=row[4],
            comments=row[5],
            quota_units_used=row[6],
        )

    def report(self, run_id: str, keyword: str) -> KeywordReport | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT body FROM reports WHERE run_id=? AND keyword=?", (run_id, keyword)
            ).fetchone()
        return KeywordReport.model_validate_json(row[0]) if row else None

    def keywords(self, run_id: str) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT keyword FROM reports WHERE run_id=? ORDER BY keyword", (run_id,)
            ).fetchall()
        return [r[0] for r in rows]

    def videos(self, run_id: str) -> list[Video]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT body FROM videos WHERE run_id=? ORDER BY keyword, rank", (run_id,)
            ).fetchall()
        return [Video.model_validate_json(r[0]) for r in rows]

    def mentions(self, run_id: str) -> list[Mention]:
        with self._conn() as c:
            rows = c.execute("SELECT body FROM mentions WHERE run_id=?", (run_id,)).fetchall()
        return [Mention.model_validate_json(r[0]) for r in rows]

    def insights(self, run_id: str) -> list[dict]:
        with self._conn() as c:
            row = c.execute("SELECT body FROM insights WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def risks(
        self, run_id: str, brand: str | None = None, severity: str | None = None, limit: int = 1000
    ) -> list[dict]:
        q, args = "SELECT body FROM risks WHERE run_id=?", [run_id]
        if brand:
            q, args = q + " AND brand=?", [*args, brand]
        if severity:
            q, args = q + " AND severity=?", [*args, severity]
        q += " ORDER BY score DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, (*args, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]
