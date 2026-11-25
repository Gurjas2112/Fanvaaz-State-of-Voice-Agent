"""Agent memory for "Ask", scoped, minimal and expiring.

Short-term (a thread): the last few question/answer turns of one conversation.
  Used to resolve follow-ups: after "How is OnePlus on battery?", the question
  "and Samsung?" inherits the aspect, and "what about camera?" inherits the brand.
  Older turns are folded into a one-line summary so the context stays small.
Long-term (preferences): the brands and aspects a user cares about, saved
  explicitly or learned from repeated questions; used as the default scope when
  a question names none.

Privacy: rows are keyed by a hash of the user id, never an email; answers are
stored PII-redacted; every row has an expiry (FANVAAZ_MEMORY_TTL_HOURS,
FANVAAZ_PREFS_TTL_DAYS) and expired rows are deleted on access.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

from .config import get_settings
from .guardrails import safe_output


def user_key(user_id: int | str) -> str:
    return hashlib.sha256(f"fanvaaz-user:{user_id}".encode()).hexdigest()[:20]


def _now() -> datetime:
    return datetime.now(UTC)


class Memory:
    def __init__(self, store):
        self.store = store
        self.s = get_settings()

    def _purge(self, c) -> None:
        now = _now().isoformat()
        c.execute("DELETE FROM memory_turns WHERE expires_at < ?", (now,))
        c.execute("DELETE FROM memory_prefs WHERE expires_at < ?", (now,))

    # ---- short-term --------------------------------------------------------------
    def remember(self, thread_id: str, ukey: str, question: str, answer: str, scope: dict) -> None:
        if self.s.memory_turns == 0:
            return
        now = _now()
        exp = (now + timedelta(hours=self.s.memory_ttl_hours)).isoformat()
        with self.store._conn() as c:
            for role, content in (("user", question), ("assistant", answer)):
                c.execute(
                    "INSERT INTO memory_turns (thread_id, user_key, role, content, scope, created_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        thread_id,
                        ukey,
                        role,
                        safe_output(content)[:1500],
                        json.dumps(scope),
                        now.isoformat(),
                        exp,
                    ),
                )

    def recall(self, thread_id: str, ukey: str) -> dict:
        """{"turns": [...recent], "summary": str, "last_scope": dict | None}"""
        with self.store._conn() as c:
            self._purge(c)
            rows = c.execute(
                "SELECT role, content, scope FROM memory_turns WHERE thread_id = ? AND user_key = ? ORDER BY id",
                (thread_id, ukey),
            ).fetchall()
        turns = [{"role": r[0], "content": r[1], "scope": json.loads(r[2] or "{}")} for r in rows]
        keep = self.s.memory_turns * 2
        older, recent = turns[:-keep], turns[-keep:]
        summary = ""
        if older:
            asked = [t["content"][:60] for t in older if t["role"] == "user"]
            summary = f"Earlier in this conversation the user asked: {'; '.join(asked[-5:])}."
        last_scope = next((t["scope"] for t in reversed(turns) if t["role"] == "user" and t["scope"]), None)
        return {"turns": recent, "summary": summary, "last_scope": last_scope}

    def forget(self, thread_id: str, ukey: str) -> int:
        with self.store._conn() as c:
            return c.execute(
                "DELETE FROM memory_turns WHERE thread_id = ? AND user_key = ?", (thread_id, ukey)
            ).rowcount

    # ---- long-term ----------------------------------------------------------------
    def set_prefs(self, ukey: str, brands: list[str], aspects: list[str]) -> dict:
        now = _now()
        exp = (now + timedelta(days=self.s.prefs_ttl_days)).isoformat()
        with self.store._conn() as c:
            for pref, value in (("brands", brands), ("aspects", aspects)):
                c.execute(
                    "INSERT INTO memory_prefs (user_key, pref, value, updated_at, expires_at) VALUES (?,?,?,?,?)"
                    " ON CONFLICT (user_key, pref) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at, expires_at = excluded.expires_at",
                    (ukey, pref, json.dumps(value[:10]), now.isoformat(), exp),
                )
        return self.prefs(ukey)

    def prefs(self, ukey: str) -> dict:
        with self.store._conn() as c:
            self._purge(c)
            rows = c.execute(
                "SELECT pref, value, expires_at FROM memory_prefs WHERE user_key = ?", (ukey,)
            ).fetchall()
        out: dict = {"brands": [], "aspects": [], "expires_at": None}
        for pref, value, exp in rows:
            out[pref] = json.loads(value)
            out["expires_at"] = exp
        return out

    def learn(self, ukey: str, scope: dict) -> None:
        """Learned preference: a brand or aspect asked about in 3+ recent turns becomes a default."""
        with self.store._conn() as c:
            rows = c.execute(
                "SELECT scope FROM memory_turns WHERE user_key = ? AND role = 'user' ORDER BY id DESC LIMIT 20",
                (ukey,),
            ).fetchall()
        brands, aspects = Counter(), Counter()
        for (sc,) in rows:
            d = json.loads(sc or "{}")
            brands.update(d.get("brands") or [])
            if d.get("aspect"):
                aspects[d["aspect"]] += 1
        current = self.prefs(ukey)
        nb = [b for b, n in brands.most_common(3) if n >= 3]
        na = [a for a, n in aspects.most_common(3) if n >= 3]
        if (nb or na) and (nb != current["brands"] or na != current["aspects"]):
            self.set_prefs(ukey, nb or current["brands"], na or current["aspects"])
