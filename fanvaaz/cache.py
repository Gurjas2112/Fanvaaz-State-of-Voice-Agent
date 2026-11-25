"""Two caches, both stored in the database so every Cloud Run instance shares them.

1. Response cache (exact). Key = sha256(model | prompt hash | user message).
   Calls run at temperature 0, so the same prompt version on the same input is
   answered from the cache at zero cost. A new prompt version changes the hash,
   which invalidates old entries by construction.

2. Verified semantic answer cache for /ask. A new question reuses a cached answer
   only when all three hold:
     * same run (the data has not changed),
     * same resolved scope (brands, keyword, aspect),
     * similar wording: cosine >= 0.92 on question embeddings, or Jaccard >= 0.8
       on normalised tokens when no embedder is configured,
   and the cached answer's cited comments still exist in the run (verification).
   This is the "verified" step from Asynchronous Verified Semantic Caching for
   Tiered LLM Architectures: a near-match is a candidate, never an answer, until
   it is checked against the current evidence.

Provider-side prefix caching (Gemini implicit caching) is the third layer: the
prompts put the long, unchanging system text first, and the ledger records the
cached prompt tokens the provider reports.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime

log = logging.getLogger("fanvaaz.cache")

SEMANTIC_COSINE = 0.92
SEMANTIC_JACCARD = 0.8
_WORD = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+")
_STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "of",
    "for",
    "to",
    "and",
    "or",
    "in",
    "on",
    "what",
    "do",
    "does",
    "how",
    "about",
    "say",
    "people",
    "it",
    "its",
    "this",
    "that",
    "with",
    "me",
    "tell",
    "i",
    "please",
}


def response_key(model: str, prompt_sha: str, user: str) -> str:
    return hashlib.sha256(f"{model}|{prompt_sha}|{user}".encode()).hexdigest()


def get_response(store, key: str) -> str | None:
    if store is None:
        return None
    try:
        with store._conn() as c:
            row = c.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
            if row:
                c.execute("UPDATE llm_cache SET hits = hits + 1 WHERE key = ?", (key,))
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        log.debug("cache read failed", exc_info=True)
        return None


def put_response(store, key: str, model: str, prompt_version: str, response: str) -> None:
    if store is None:
        return
    try:
        with store._conn() as c:
            c.execute(
                "INSERT INTO llm_cache (key, model, prompt_version, response, created_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT (key) DO NOTHING",
                (key, model, prompt_version, response, datetime.now(UTC).isoformat(timespec="seconds")),
            )
    except Exception:  # noqa: BLE001
        log.debug("cache write failed", exc_info=True)


# ---- semantic answer cache -----------------------------------------------------------
def norm_tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if t not in _STOP}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def scope_key(scope: dict) -> str:
    return json.dumps(
        {
            "brands": sorted(scope.get("brands") or []),
            "keyword": scope.get("keyword"),
            "aspect": scope.get("aspect"),
            "intent": scope.get("intent"),  # a praise question never reuses a complaint answer
        },
        sort_keys=True,
    )


def find_answer(
    store, run_id: str, scope: dict, question: str, embedding: list[float] | None, valid_ids: set[str]
) -> tuple[dict, float] | None:
    """A verified cached answer and its similarity, or None."""
    if store is None:
        return None
    try:
        with store._conn() as c:
            rows = c.execute(
                "SELECT id, question, embedding, answer FROM answer_cache WHERE run_id = ? AND scope = ?"
                " ORDER BY id DESC LIMIT 50",
                (run_id, scope_key(scope)),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return None
    q_tokens = norm_tokens(question)
    for row_id, cached_q, emb_json, answer_json in rows:
        if embedding is not None and emb_json:
            sim = cosine(embedding, json.loads(emb_json))
            ok = sim >= SEMANTIC_COSINE
        else:
            sim = jaccard(q_tokens, norm_tokens(cached_q))
            ok = sim >= SEMANTIC_JACCARD
        if not ok:
            continue
        answer = json.loads(answer_json)
        cited = {s["comment_id"] for s in answer.get("sources", [])}
        if not cited <= valid_ids:  # verification: every cited comment must still exist
            continue
        with store._conn() as c:
            c.execute("UPDATE answer_cache SET hits = hits + 1 WHERE id = ?", (row_id,))
        return answer, round(sim, 3)
    return None


def put_answer(
    store, run_id: str, scope: dict, question: str, embedding: list[float] | None, answer: dict
) -> None:
    if store is None:
        return
    try:
        with store._conn() as c:
            c.execute(
                "INSERT INTO answer_cache (run_id, scope, question, embedding, answer, created_at) VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    scope_key(scope),
                    question[:1000],
                    json.dumps(embedding) if embedding is not None else None,
                    json.dumps(answer, ensure_ascii=False),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
    except Exception:  # noqa: BLE001
        log.debug("answer cache write failed", exc_info=True)
