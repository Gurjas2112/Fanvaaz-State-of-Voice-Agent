"""Persistence for runs, profile and triage, on SQLite or PostgreSQL (see db.py).

Runs are append-only: every collection is a new run, the API serves the latest
one, and older runs stay for comparison. All SQL is portable; db.py picks the
engine from FANVAAZ_DATABASE_URL (Postgres) or FANVAAZ_DB_PATH (SQLite).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from .db import Database
from .models import KeywordReport, Mention, RunInfo, Video

RUN_COLS = "run_id, source, created_at, keywords, videos, comments, quota_units_used, profile"


def _run_from_row(row) -> RunInfo:
    prof = json.loads(row[7]) if len(row) > 7 and row[7] else {}
    extra = {}
    if prof:
        extra = {"profile_id": prof["id"], "focus_brand": prof["focus_brand"], "brands": prof["brands"]}
    return RunInfo(
        run_id=row[0],
        source=row[1],
        created_at=row[2],
        keywords=json.loads(row[3]),
        videos=row[4],
        comments=row[5],
        quota_units_used=row[6],
        **extra,
    )


class Store:
    def __init__(self, target: str | Path):
        """`target` is a SQLite file path or a postgresql:// URL."""
        self.db = Database(target)
        self.path = Path(target) if self.db.dialect == "sqlite" else None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings) -> Store:
        return cls(settings.database_url or settings.db_path)

    @property
    def dialect(self) -> str:
        return self.db.dialect

    def _conn(self):
        return self.db.conn()

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
                "INSERT INTO runs (run_id, source, created_at, keywords, videos, comments, quota_units_used, profile)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    info.run_id,
                    info.source,
                    info.created_at,
                    json.dumps(info.keywords),
                    info.videos,
                    info.comments,
                    info.quota_units_used,
                    json.dumps(
                        {"id": info.profile_id, "focus_brand": info.focus_brand, "brands": info.brands}
                    ),
                ),
            )
            c.executemany(
                "INSERT INTO videos VALUES (?,?,?,?,?) ON CONFLICT (run_id, keyword, video_id)"
                " DO UPDATE SET rank = excluded.rank, body = excluded.body",
                [(info.run_id, v.keyword, v.video_id, v.rank, v.model_dump_json()) for v in videos],
            )
            c.executemany(
                "INSERT INTO mentions VALUES (?,?,?,?)",
                [(info.run_id, m.keyword, m.brand, m.model_dump_json()) for m in mentions],
            )
            c.executemany(
                "INSERT INTO reports VALUES (?,?,?) ON CONFLICT (run_id, keyword) DO UPDATE SET body = excluded.body",
                [(info.run_id, k, r.model_dump_json()) for k, r in reports.items()],
            )
            c.execute(
                "INSERT INTO insights VALUES (?,?) ON CONFLICT (run_id) DO UPDATE SET body = excluded.body",
                (info.run_id, json.dumps(insights)),
            )
            c.executemany(
                "INSERT INTO risks VALUES (?,?,?,?,?)",
                [
                    (info.run_id, r["brand"], r["severity"], r["score"], json.dumps(r, ensure_ascii=False))
                    for r in (risks or [])
                ],
            )

    def latest_run(self, profile_id: str | None = None) -> RunInfo | None:
        """The newest run, optionally of one profile (runs store the profile they used)."""
        if profile_id:
            return next(iter(self.runs(200, profile_id=profile_id)), None)
        with self._conn() as c:
            row = c.execute(f"SELECT {RUN_COLS} FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        return _run_from_row(row)

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
    def runs(self, limit: int = 30, profile_id: str | None = None) -> list[RunInfo]:
        # Filtered in Python: the profile is a JSON column, and run history is small.
        fetch = limit if not profile_id else max(limit * 10, 500)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {RUN_COLS} FROM runs ORDER BY created_at DESC LIMIT ?", (fetch,)
            ).fetchall()
        out = [_run_from_row(r) for r in rows]
        if profile_id:
            out = [r for r in out if r.profile_id == profile_id]
        return out[:limit]

    def previous_run(self, run_id: str, same_source: bool = True) -> RunInfo | None:
        """The run before this one, of the same profile (and source): what 'since last run' compares."""
        history = self.runs(500)
        cur = next((r for r in history if r.run_id == run_id), None)
        for r in history:
            if (
                cur
                and r.created_at < cur.created_at
                and (not same_source or r.source == cur.source)
                and (r.profile_id == cur.profile_id or not cur.profile_id)
            ):
                return r
        return None

    # ---- risk triage: survives across runs, keyed by comment + brand -------

    def set_risk_status(
        self, comment_id: str, brand: str, status: str, user_email: str, note: str = ""
    ) -> dict:

        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO risk_status VALUES (?,?,?,?,?,?) ON CONFLICT (comment_id, brand) DO UPDATE SET"
                " status = excluded.status, note = excluded.note, user_email = excluded.user_email,"
                " updated_at = excluded.updated_at",
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
            rows = c.execute(
                "SELECT comment_id, brand, status, note, user_email, updated_at FROM risk_status"
            ).fetchall()
        return {
            (r[0], r[1]): {"status": r[2], "note": r[3], "updated_by": r[4], "updated_at": r[5]} for r in rows
        }

    # ---- tracking profiles chosen by an admin (several per workspace) ------
    @staticmethod
    def _profile_row(row) -> dict:
        return {
            "profile": json.loads(row[0]),
            "updated_by": row[1],
            "updated_at": row[2],
            "is_default": bool(row[3]),
        }

    def profiles(self) -> list[dict]:
        """Every saved profile, the default first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT body, updated_by, updated_at, is_default FROM profiles ORDER BY is_default DESC, id"
            ).fetchall()
        return [self._profile_row(r) for r in rows]

    def saved_profile(self, profile_id: str | None = None) -> dict | None:
        """One saved profile by id, or the workspace default."""
        sql = "SELECT body, updated_by, updated_at, is_default FROM profiles WHERE "
        sql += "id = ?" if profile_id else "is_default = 1"
        with self._conn() as c:
            row = c.execute(sql, (profile_id,) if profile_id else ()).fetchone()
        return self._profile_row(row) if row else None

    def save_profile(self, body: dict, user_email: str, make_default: bool = False) -> None:
        """Add or update a profile (by its id). The first profile saved becomes the default."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn() as c:
            has_default = c.execute("SELECT 1 FROM profiles WHERE is_default = 1").fetchone() is not None
            c.execute(
                "INSERT INTO profiles (id, body, updated_by, updated_at, is_default) VALUES (?,?,?,?,0)"
                " ON CONFLICT (id) DO UPDATE SET body = excluded.body, updated_by = excluded.updated_by,"
                " updated_at = excluded.updated_at",
                (body["id"], json.dumps(body, ensure_ascii=False), user_email, now),
            )
            if make_default or not has_default:
                c.execute(
                    "UPDATE profiles SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (body["id"],)
                )

    def set_default_profile(self, profile_id: str) -> bool:
        with self._lock, self._conn() as c:
            if c.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone() is None:
                return False
            c.execute("UPDATE profiles SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (profile_id,))
        return True

    def delete_profile(self, profile_id: str) -> str:
        """'deleted', 'missing' or 'is_default'. Past runs of a deleted profile stay in the history."""
        with self._lock, self._conn() as c:
            row = c.execute("SELECT is_default FROM profiles WHERE id = ?", (profile_id,)).fetchone()
            if row is None:
                return "missing"
            if row[0]:
                return "is_default"
            c.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        return "deleted"
