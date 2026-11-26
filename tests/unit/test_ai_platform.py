"""The AI platform around the agents: governance, the LLM gateway, caching, tokens,
context budgets, schemas, hybrid retrieval, memory, checkpoint resume and migrations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from fanvaaz import ai, cache, context, obs, prompts, tokens
from fanvaaz.config import get_settings
from fanvaaz.governance import Governance
from fanvaaz.memory import Memory, user_key
from fanvaaz.retrieval import Chunk, HybridRetriever
from fanvaaz.schemas import AnswerOut, RewriteOut, SentimentBatchOut, json_schema


class ScriptedLLM:
    """Returns replies in order; records what it was sent."""

    def __init__(self, *replies, cacheable=False):
        self.replies, self.sent, self.cacheable = list(replies), [], cacheable

    def complete(self, system, user, *, json_mode=False):
        self.sent.append(user)
        return self.replies.pop(0) if self.replies else self.sent[-1]


@pytest.fixture
def bound(seeded_store):
    obs.bind(seeded_store)
    obs.set_run(None)
    yield seeded_store


# ---- prompts and schemas -----------------------------------------------------------
def test_prompt_registry_versions_and_hashes():
    p = prompts.get("ask", focus="OnePlus", category="smartphones")
    assert p.version == "2.0" and len(p.sha) == 12 and "{focus}" not in p.system and "OnePlus" in p.system
    assert prompts.get("ask", focus="X", category="y").sha != p.sha  # the hash is of the exact text
    ids = {c["id"] for c in prompts.catalog()}
    assert ids == {"sentiment", "rewrite", "ask", "plan"}


def test_json_schema_is_inlined_for_providers():
    schema = json.dumps(json_schema(SentimentBatchOut))
    assert "$ref" not in schema and "$defs" not in schema and "title" not in schema


# ---- the gateway ---------------------------------------------------------------------
def test_gateway_repairs_an_invalid_reply_once(bound):
    llm = ScriptedLLM("not json at all", json.dumps({"items": [{"id": "headline", "text": "Better words."}]}))
    res = ai.call(llm, prompts.get("rewrite"), "{}", task="rewrite", schema=RewriteOut)
    assert res.ok and isinstance(res.parsed, RewriteOut) and len(llm.sent) == 2
    assert "did not match the required JSON format" in llm.sent[1]


def test_gateway_gives_up_after_one_repair(bound):
    llm = ScriptedLLM("nope", "still nope")
    res = ai.call(llm, prompts.get("ask", focus="A", category="b"), "q", task="ask", schema=AnswerOut)
    assert not res.ok and res.outcome == "invalid" and len(llm.sent) == 2


def test_kill_switch_and_task_allow_list(bound, monkeypatch):
    llm = ScriptedLLM('{"answer": "x"}')
    monkeypatch.setattr(get_settings(), "llm_enabled", False)
    assert ai.call(llm, prompts.get("plan", category="c"), "q", task="plan").outcome == "refused:kill_switch"
    monkeypatch.setattr(get_settings(), "llm_enabled", True)
    assert ai.call(llm, prompts.get("plan", category="c"), "q", task="poetry").outcome.startswith(
        "refused:task_not_allowed"
    )
    assert llm.sent == []  # refused calls never reach the model


def test_circuit_breaker_stops_calls_after_a_billing_error(bound):
    """Found on a real Gemini key: a 402 (prepaid credits depleted) on every call. One hard
    failure opens the circuit; the rest of the run is refused without network calls."""
    import httpx

    from fanvaaz.config import Settings
    from fanvaaz.llm import OpenAICompatLLM

    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        return httpx.Response(
            402, json={"error": {"code": 402, "message": "Your prepayment credits are depleted."}}
        )

    llm = OpenAICompatLLM(
        Settings(llm_base_url="https://llm.test/v1", llm_api_key="k", llm_model="gemini-flash-latest"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    first = ai.call(llm, prompts.get("rewrite"), "a", task="rewrite")
    second = ai.call(llm, prompts.get("rewrite"), "b", task="rewrite")
    assert not first.ok and first.outcome == "error"
    assert second.outcome == "refused:circuit_open" and hits["n"] == 1
    assert "prepayment credits" in llm.circuit_open


def test_model_allow_list(bound, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_model", "some-unvetted-model")
    gov = Governance(get_settings(), bound)
    assert gov.authorize("ask", "some-unvetted-model", 10).reason.startswith("model_not_allowed")
    assert gov.model_allowed("gemini-flash-latest") and gov.model_allowed("gemini-2.5-flash")


def test_context_and_cost_budgets(bound, monkeypatch):
    gov = Governance(get_settings(), bound)
    assert gov.authorize("ask", "gemini-flash-latest", 999_999).reason.startswith("context_budget")
    monkeypatch.setattr(get_settings(), "max_cost_per_day_usd", 0.0)
    assert gov.authorize("ask", "gemini-flash-latest", 10).reason.startswith("day_budget")


def test_ledger_records_estimates_and_costs(bound):
    before = obs.ai_ops(bound)["calls"]["total"]
    ai.call(ScriptedLLM("plain answer"), prompts.get("rewrite"), "hello world", task="rewrite")
    ops = obs.ai_ops(bound)
    assert ops["calls"]["total"] == before + 1
    last = ops["recent"][0]
    assert last["prompt"] == "rewrite@2.0" and last["tokens"] > 0 and last["cost_usd"] > 0


def test_response_cache_serves_repeat_calls_for_cacheable_clients(bound):
    llm = ScriptedLLM(json.dumps({"items": []}), cacheable=True)
    user = f"cache-probe-{datetime.now(UTC).timestamp()}"
    first = ai.call(llm, prompts.get("rewrite"), user, task="rewrite", schema=RewriteOut)
    second = ai.call(llm, prompts.get("rewrite"), user, task="rewrite", schema=RewriteOut)
    assert first.ok and not first.cached and second.cached and len(llm.sent) == 1


def test_fakes_are_never_served_from_cache(bound):
    a, b = ScriptedLLM("one"), ScriptedLLM("two")
    assert ai.call(a, prompts.get("rewrite"), "same", task="rewrite").text == "one"
    assert ai.call(b, prompts.get("rewrite"), "same", task="rewrite").text == "two"


# ---- tokens and context ---------------------------------------------------------------
def test_token_estimates_and_cost():
    assert tokens.estimate("abcd" * 100) == 100
    assert tokens.estimate("वनप्लस") == 3  # Devanagari: ~2 chars per token
    assert tokens.cost_usd(1_000_000, 0, 1_000_000, 0.30, 2.50, 0.075) == pytest.approx(0.075)
    kept, used = tokens.fit(["a" * 40, "b" * 40, "c" * 40], budget=25)
    assert len(kept) == 2 and used == 20


def test_context_builder_respects_the_budget_and_cites():
    sources = [
        {"comment_id": f"id{i}", "video_id": "v", "keyword": "k", "text": f"comment {i} " * 30}
        for i in range(20)
    ]
    ctx = context.build(["OnePlus SoV 6.7%"], sources, budget_tokens=400)
    assert ctx.report["used_tokens"] <= 400 and ctx.report["comments_dropped"] > 0
    assert list(ctx.citations)[0] == "c1" and json.loads(ctx.data)["stats"] == ["OnePlus SoV 6.7%"]


def test_context_redacts_pii_before_the_model_sees_it():
    ctx = context.build(
        [], [{"comment_id": "1", "video_id": "v", "keyword": "k", "text": "call me on 9876543210 or a@b.com"}]
    )
    assert "9876543210" not in ctx.data and "a@b.com" not in ctx.data


# ---- retrieval --------------------------------------------------------------------------
def _chunks():
    return [
        Chunk("c1", "v1", "k", "OnePlus battery lasts two days", ["OnePlus"], like_count=50, rank=1),
        Chunk("c2", "v1", "k", "Samsung camera is amazing at night", ["Samsung"], like_count=5, rank=1),
        Chunk("c3", "v2", "k", "OnePlus charging is very fast", ["OnePlus"], like_count=0, rank=9),
        Chunk("c4", "v2", "k", "battery battery battery", [], like_count=0, rank=30),
    ]


class FakeEmbedder:
    """Vectors from a tiny vocabulary: enough to test the vector leg and fusion."""

    VOCAB = ["battery", "charging", "camera", "night", "fast", "days"]

    def embed(self, texts, node=""):
        return [[float(w in t.lower()) for w in self.VOCAB] for t in texts]


def test_hybrid_retrieval_fuses_both_legs_and_reranks():
    emb = FakeEmbedder()
    chunks = _chunks()
    vectors = {c.doc_id: v for c, v in zip(chunks, emb.embed([c.text for c in chunks]), strict=True)}
    r = HybridRetriever(chunks, vectors, emb)
    hits = r.search(
        "how long does the battery last when charging",
        k=3,
        must_brand="OnePlus",
        aspect_words=["battery", "charging"],
    )
    assert [h.chunk.doc_id for h in hits][:2] == ["c1", "c3"]
    assert hits[0].lexical_rank and hits[0].vector_rank and hits[0].features["brand"] == 1.0
    lexical = r.search("battery", k=3, mode="lexical")
    assert all(h.vector_rank is None for h in lexical)


def test_retrieval_eval_reports_recall_and_mrr():
    from fanvaaz.retrieval import evaluate

    emb = FakeEmbedder()
    chunks = _chunks()
    vectors = {c.doc_id: v for c, v in zip(chunks, emb.embed([c.text for c in chunks]), strict=True)}
    out = evaluate(
        HybridRetriever(chunks, vectors, emb),
        [{"question": "night camera", "relevant": ["c2"]}, {"question": "fast charging", "relevant": ["c3"]}],
        k=2,
    )
    assert set(out) == {"lexical", "vector", "hybrid"} and out["hybrid"]["mrr"] == 1.0


class FakeEmbedder768(FakeEmbedder):
    """Same vocabulary vectors, padded to the 768 dims the pgvector column expects."""

    def embed(self, texts, node=""):
        return [v + [0.0] * (768 - len(v)) for v in super().embed(texts)]


def test_vector_leg_on_the_configured_database(seeded_store, latest):
    """Index real comments with vectors and search them: pgvector SQL on Postgres, numpy on SQLite."""
    from fanvaaz.retrieval import index_run, load_chunks

    videos = [v for v in latest["videos"] if v.comments][:8]
    emb = FakeEmbedder768()
    stats = index_run(seeded_store, "vectest-run", videos, emb)
    assert stats["embedded"] and stats["chunks"] > 20
    chunks, vecs = load_chunks(seeded_store, "vectest-run")
    assert vecs  # vectors present (SQLite: values; Postgres: marker, searched in SQL)
    r = HybridRetriever(chunks, vecs, emb, seeded_store, "vectest-run")
    hits = r.search("battery and charging", k=5, mode="vector")
    assert hits and all(h.vector_rank for h in hits)
    assert any("batter" in h.chunk.text.lower() or "charg" in h.chunk.text.lower() for h in hits)


# ---- semantic answer cache -------------------------------------------------------------
def test_semantic_cache_verifies_citations(bound):
    scope = {"brands": ["Samsung"], "keyword": "all", "aspect": "camera"}
    ans = {
        "answer": "x",
        "mode": "extractive",
        "sources": [{"comment_id": "real1"}],
        "citations": ["c1"],
        "scope": scope,
    }
    run = f"probe-{datetime.now(UTC).timestamp()}"
    cache.put_answer(bound, run, scope, "what about the samsung camera", None, ans)
    assert cache.find_answer(bound, run, scope, "What about the Samsung camera?", None, {"real1"})
    # Same question, but the cited comment no longer exists in the run: not served.
    assert cache.find_answer(bound, run, scope, "What about the Samsung camera?", None, {"other"}) is None
    # Different scope: not served.
    assert (
        cache.find_answer(bound, run, {**scope, "aspect": "battery"}, "samsung camera", None, {"real1"})
        is None
    )


# ---- memory --------------------------------------------------------------------------------
def test_memory_expires_and_is_keyed_by_hash(bound):
    mem = Memory(bound)
    uk = user_key(12345)
    assert "12345" not in uk and len(uk) == 20
    mem.remember(
        "abcd1234", uk, "How is Samsung on battery?", "fine", {"brands": ["Samsung"], "aspect": "battery"}
    )
    assert mem.recall("abcd1234", uk)["last_scope"]["brands"] == ["Samsung"]
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with bound._conn() as c:
        c.execute("UPDATE memory_turns SET expires_at = ? WHERE thread_id = ?", (past, "abcd1234"))
    assert mem.recall("abcd1234", uk)["turns"] == []


def test_preferences_are_learned_from_repeated_questions(bound):
    mem = Memory(bound)
    uk = user_key("learner")
    for i in range(3):
        mem.remember(f"t{i}aaaaaa", uk, "q", "a", {"brands": ["Vivo"], "aspect": "camera"})
    mem.learn(uk, {})
    assert mem.prefs(uk)["brands"] == ["Vivo"] and mem.prefs(uk)["aspects"] == ["camera"]


# ---- harness: checkpointed graph resumes after a failure ------------------------------------
def test_collection_graph_resumes_from_its_checkpoint(seeded_store, monkeypatch):
    from pathlib import Path

    from fanvaaz import insights as insights_mod
    from fanvaaz.pipeline import new_run_id, resume, run

    snapshot = Path(__file__).parents[1] / "fixtures" / "youtube_snapshot" / "smartphones-india.json"
    real_build = insights_mod.build
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider outage")
        return real_build(*a, **k)

    monkeypatch.setattr(insights_mod, "build", flaky)
    rid = new_run_id()
    with pytest.raises(RuntimeError, match="provider outage"):
        run(source="replay", snapshot=snapshot, store=seeded_store, use_llm=False, run_id=rid)
    assert all(r.run_id != rid for r in seeded_store.runs(50))  # nothing half-saved
    info = resume(rid, store=seeded_store)
    assert info.run_id == rid and info.comments > 3000 and calls["n"] == 2
    actions = [
        e["action"]
        for e in __import__("fanvaaz.governance", fromlist=["audit_log"]).audit_log(seeded_store, 20)
    ]
    assert "run.failed" in actions and "run.resume" in actions


# ---- database migrations --------------------------------------------------------------------
def test_migrations_are_recorded_and_idempotent(tmp_path):
    from fanvaaz.db import MIGRATIONS, Database

    db = Database(tmp_path / "m.db")
    assert db.schema_version() == MIGRATIONS[-1].version
    assert db.migrate() == []  # second run applies nothing


def test_old_sqlite_files_are_upgraded(tmp_path):
    import sqlite3

    from fanvaaz.db import Database

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, source TEXT NOT NULL, created_at TEXT NOT NULL,"
        " keywords TEXT NOT NULL, videos INTEGER NOT NULL, comments INTEGER NOT NULL,"
        " quota_units_used INTEGER NOT NULL DEFAULT 0)"
    )
    raw.commit()
    raw.close()
    db = Database(path)
    with db.conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(runs)").fetchall()}
    assert "profile" in cols and db.schema_version() >= 3


# ---- embedding input hygiene (a blank comment made Gemini 400 the whole batch) ------
def test_blank_comments_are_not_indexed_and_embed_never_sends_empty_input():
    import httpx

    from fanvaaz.config import Settings
    from fanvaaz.llm import OpenAICompatLLM
    from fanvaaz.models import Comment, Video
    from fanvaaz.retrieval import chunks_from_videos

    v = Video(video_id="v1", keyword="k", rank=1, title="t")
    v.comments = [Comment(comment_id="a", text="   "), Comment(comment_id="b", text="great battery")]
    chunks, _ = chunks_from_videos([v])
    assert [c.doc_id for c in chunks] == ["b"]

    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        sent.append(body["input"])
        if any(not t.strip() for t in body["input"]):
            return httpx.Response(400, json={"error": {"message": "empty content"}})
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(body["input"])]}
        )

    s = Settings(llm_base_url="https://llm.test/v1", llm_api_key="k")
    llm = OpenAICompatLLM(s, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(llm.embed(["", "ok"], dims=2)) == 2
    assert all(t.strip() for t in sent[0])


def test_provider_error_message_is_kept():
    import httpx

    from fanvaaz.config import Settings
    from fanvaaz.llm import OpenAICompatLLM

    def handler(req):
        return httpx.Response(400, json={"error": {"message": "model not found for embedContent"}})

    s = Settings(llm_base_url="https://llm.test/v1", llm_api_key="k")
    llm = OpenAICompatLLM(s, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.HTTPStatusError, match="model not found"):
        llm.embed(["x"])


def test_relabel_batches_run_in_parallel_and_apply_to_the_right_mentions():
    import threading

    from fanvaaz.extract import llm_relabel
    from fanvaaz.models import Mention

    threads = set()

    class EchoLLM:
        """Labels each item from its own id, so a mix-up between parallel batches shows."""

        cacheable = False

        def complete(self, system, user, *, json_mode=False):
            threads.add(threading.get_ident())
            items = json.loads(user[user.index("{") :])["items"]
            return json.dumps(
                {
                    "items": [
                        {
                            "id": it["id"],
                            "labels": [
                                {
                                    "brand": b,
                                    "sentiment": "positive"
                                    if int(it["id"].split("|")[0]) % 2
                                    else "negative",
                                }
                                for b in it["brands"]
                            ],
                        }
                        for it in items
                    ]
                }
            )

    mentions = [
        Mention(
            video_id="v",
            keyword="k",
            source="comment",
            source_id=str(i),
            brand="OnePlus",
            sentiment="neutral",
            confidence=0.0,
        )
        for i in range(120)
    ]
    texts = {f"{i}|comment": f"comment {i}" for i in range(120)}
    out = llm_relabel(mentions, texts, EchoLLM(), batch_size=10, only_uncertain=True, workers=4)
    assert all(m.sentiment == ("positive" if int(m.source_id) % 2 else "negative") for m in out)
    assert len(threads) > 1


def test_logs_never_carry_api_keys_or_bearer_tokens(capsys):
    import logging

    obs.configure_logging()
    try:
        logging.getLogger("fanvaaz.test").info(
            "GET https://www.googleapis.com/youtube/v3/search?q=x&key=AIzaSECRETSECRET123 auth=%s",
            "Bearer tok_SECRET_456",
        )
        logging.getLogger("httpx").info("HTTP Request: GET ...&key=AIzaHIDDEN")
        err = capsys.readouterr().err
        assert "SECRET" not in err and "HIDDEN" not in err
        assert "key=[REDACTED]" in err and "Bearer [REDACTED]" in err
    finally:
        logging.getLogger().handlers.clear()
