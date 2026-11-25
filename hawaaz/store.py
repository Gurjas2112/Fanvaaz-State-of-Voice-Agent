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

    # ---- run history and day-over-day comparison --------------------------
    def runs(self, limit: int = 30) -> list[RunInfo]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [
            RunInfo(
                run_id=r[0],
                source=r[1],
                created_at=r[2],
                keywords=json.loads(r[3]),
                videos=r[4],
                comments=r[5],
                quota_units_used=r[6],
            )
            for r in rows
        ]

    def previous_run(self, run_id: str, same_source: bool = True) -> RunInfo | None:
        cur = next((r for r in self.runs(200) if r.run_id == run_id), None)
        for r in self.runs(200):
            if cur and r.created_at < cur.created_at and (not same_source or r.source == cur.source):
                return r
        return None

    # ---- risk triage: survives across runs, keyed by comment + brand -------
    def _ensure_triage(self, c) -> None:
        c.execute("""CREATE TABLE IF NOT EXISTS risk_status (
            comment_id TEXT NOT NULL, brand TEXT NOT NULL, status TEXT NOT NULL,
            note TEXT, user_email TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (comment_id, brand))""")

    def set_risk_status(
        self, comment_id: str, brand: str, status: str, user_email: str, note: str = ""
    ) -> dict:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn() as c:
            self._ensure_triage(c)
            c.execute(
                "INSERT OR REPLACE INTO risk_status VALUES (?,?,?,?,?,?)",
                (comment_id, brand, status, note[:300], user_email, now),
            )
        return {
            "comment_id": comment_id,
            "brand": brand,
            "status": status,
            "note": note[:300],
            "updated_by": user_email,
            "updated_at": now,
        }

    def risk_statuses(self) -> dict[tuple[str, str], dict]:
        with self._conn() as c:
            self._ensure_triage(c)
            rows = c.execute("SELECT * FROM risk_status").fetchall()
        return {
            (r[0], r[1]): {"status": r[2], "note": r[3], "updated_by": r[4], "updated_at": r[5]} for r in rows
        }
