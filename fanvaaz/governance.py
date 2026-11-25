"""AI governance: the policy, the gates that enforce it, and the audit trail.

Every LLM call passes `Governance.authorize` before it is sent:
  1. kill switch      FANVAAZ_LLM_ENABLED=false turns every LLM step off
  2. task allow-list  the task must be listed under `llm_may` in policy.yaml
  3. model allow-list the configured model must match `models.chat_allowed`
  4. context budget   the estimated prompt must fit FANVAAZ_MAX_PROMPT_TOKENS
  5. cost budgets     spend this run and today must stay under the caps
A refused call is not an error: the caller keeps its rule-based result, and the
refusal is recorded in the LLM ledger with its reason.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml

from .config import Settings, get_settings

log = logging.getLogger("fanvaaz.governance")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


@lru_cache(maxsize=4)
def load_policy(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"governance policy not found: {p}")
    policy = yaml.safe_load(p.read_text(encoding="utf-8"))
    for key in ("models", "llm_may", "llm_may_not"):
        if key not in policy:
            raise ValueError(f"policy is missing '{key}'")
    return policy


class Governance:
    def __init__(self, settings: Settings | None = None, store=None):
        self.s = settings or get_settings()
        self.policy = load_policy(str(self.s.policy_path))
        self.store = store

    # ---- static checks -----------------------------------------------------
    def model_allowed(self, model: str, kind: str = "chat") -> bool:
        patterns = self.policy["models"].get(f"{kind}_allowed", [])
        return any(fnmatch.fnmatch(model, p) for p in patterns)

    def task_allowed(self, task: str) -> bool:
        return task in self.policy["llm_may"]

    # ---- spend -------------------------------------------------------------
    def spend(self, run_id: str | None = None) -> tuple[float, float]:
        """(USD spent on this run, USD spent today UTC), from the LLM ledger."""
        if self.store is None:
            return 0.0, 0.0
        today = datetime.now(UTC).date().isoformat()
        with self.store._conn() as c:
            day = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE ts >= ?", (today,)
            ).fetchone()
            run = (0.0,)
            if run_id:
                run = c.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id = ?", (run_id,)
                ).fetchone()
        return float(run[0] or 0), float(day[0] or 0)

    def authorize(self, task: str, model: str, est_tokens: int, run_id: str | None = None) -> Decision:
        if not self.s.llm_enabled:
            return Decision(False, "kill_switch")
        if not self.task_allowed(task):
            return Decision(False, f"task_not_allowed:{task}")
        if not self.model_allowed(model):
            return Decision(False, f"model_not_allowed:{model}")
        if est_tokens > self.s.max_prompt_tokens:
            return Decision(False, f"context_budget:{est_tokens}>{self.s.max_prompt_tokens}")
        run_spend, day_spend = self.spend(run_id)
        if run_id and run_spend >= self.s.max_cost_per_run_usd:
            return Decision(False, f"run_budget:{run_spend:.4f}")
        if day_spend >= self.s.max_cost_per_day_usd:
            return Decision(False, f"day_budget:{day_spend:.4f}")
        return Decision(True)

    def summary(self) -> dict:
        run_spend, day_spend = self.spend()
        return {
            "policy_version": self.policy.get("version"),
            "llm_enabled": self.s.llm_enabled,
            "llm_configured": self.s.llm_configured,
            "model": self.s.llm_model,
            "model_allowed": self.model_allowed(self.s.llm_model),
            "embed_model": self.s.embed_model,
            "embed_model_allowed": self.model_allowed(self.s.embed_model, "embeddings"),
            "tasks": list(self.policy["llm_may"]),
            "llm_may_not": self.policy["llm_may_not"],
            "human_in_the_loop": self.policy.get("human_in_the_loop", {}),
            "budgets": {
                "per_run_usd": self.s.max_cost_per_run_usd,
                "per_day_usd": self.s.max_cost_per_day_usd,
                "max_prompt_tokens": self.s.max_prompt_tokens,
                "spent_today_usd": round(day_spend, 6),
            },
        }


# ---- audit trail ---------------------------------------------------------------
def audit(store, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
    """Append-only record of who changed what. Never raises: auditing must not break the action."""
    if store is None:
        return
    try:
        with store._conn() as c:
            c.execute(
                "INSERT INTO audit_log (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    actor,
                    action,
                    target[:200],
                    json.dumps(detail or {}, ensure_ascii=False)[:4000],
                ),
            )
    except Exception:  # noqa: BLE001
        log.exception("audit write failed for %s %s", actor, action)


def audit_log(store, limit: int = 100) -> list[dict]:
    with store._conn() as c:
        rows = c.execute(
            "SELECT ts, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {"ts": r[0], "actor": r[1], "action": r[2], "target": r[3], "detail": json.loads(r[4] or "{}")}
        for r in rows
    ]
