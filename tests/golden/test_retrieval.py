"""Ranking quality on real comments: does the retriever put the right comments first?

ask_real.jsonl: 8 real questions over the recorded YouTube snapshot, each with the comment
ids a human judged relevant. Judgments were pooled (the top candidates of the lexical and
the re-ranked hybrid retriever, judged by hand), the standard IR practice; comments neither
system retrieved count as not relevant, so absolute recall is an upper bound.

Two tests:
* offline (always runs): plain BM25 vs the full pipeline without vectors (brand-free query,
  intent-aware candidates, feature re-rank).
* with embeddings (pytest -m llm, needs LLM_EVAL_* keys): BM25 vs vector-only vs the full
  hybrid (BM25 + pgvector-style cosine fused by RRF, then re-ranked), all on real Gemini
  embeddings. Vectors are cached in reports/embed_cache.db, so a re-run costs nothing.
  Caveat: the pool was judged before the vector leg existed, so comments only the vector
  leg finds count as not relevant; vector-only numbers are a lower bound.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fanvaaz.ask import Answerer, aspect_words
from fanvaaz.graph.ask_graph import intent_of, without_brands
from fanvaaz.retrieval import Embedder, HybridRetriever, chunks_from_videos

GOLDEN = Path(__file__).parent / "ask_real.jsonl"
K = 5


def _rows() -> list[dict]:
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]


def _metrics(ranked: list[list[str]], relevant: list[set[str]]) -> dict:
    recalls, rrs, precisions = [], [], []
    for got, rel in zip(ranked, relevant, strict=True):
        top = got[:K]
        recalls.append(len(rel & set(top)) / min(len(rel), K))
        precisions.append(len(rel & set(top)) / K)
        rrs.append(next((1 / i for i, cid in enumerate(top, 1) if cid in rel), 0.0))
    n = len(ranked)
    return {
        f"recall@{K}": round(sum(recalls) / n, 3),
        f"precision@{K}": round(sum(precisions) / n, 3),
        "mrr": round(sum(rrs) / n, 3),
    }


def _ids(hits) -> list[str]:
    return [h.chunk.doc_id for h in hits]


def _simple(retriever: HybridRetriever, rows: list[dict], mode: str) -> list[list[str]]:
    """One leg alone, on the raw question (lexical = plain BM25, vector = cosine only)."""
    return [_ids(retriever.search(r["question"], k=K, must_brand=r["brand"], mode=mode)) for r in rows]


def _full(retriever: HybridRetriever, rows: list[dict], sentiment_of: dict) -> list[list[str]]:
    """The production Ask path: brand-free query + aspect words, intent-aware candidates, re-rank."""
    out = []
    for r in rows:
        words = aspect_words(r["aspect"])
        query = without_brands(r["question"]) + " " + " ".join(words[:4])
        hits = retriever.search(
            query,
            k=K,
            must_brand=r["brand"],
            aspect_words=words,
            want_sentiment=intent_of(r["question"]),
            sentiment_of=sentiment_of,
        )
        out.append(_ids(hits))
    return out


@pytest.mark.golden
def test_reranked_retrieval_beats_plain_bm25(latest, reports_dir):
    a = Answerer(latest["videos"], latest["mentions"], latest["reports"])
    rows = _rows()
    relevant = [set(r["relevant"]) for r in rows]
    report = {
        "questions": len(rows),
        "k": K,
        "bm25": _metrics(_simple(a.retriever, rows, "lexical"), relevant),
        "hybrid_reranked": _metrics(_full(a.retriever, rows, a.sentiment_of), relevant),
    }
    (reports_dir / "retrieval.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert report["hybrid_reranked"]["mrr"] >= report["bm25"]["mrr"]
    assert report["hybrid_reranked"][f"precision@{K}"] >= report["bm25"][f"precision@{K}"]
    assert report["hybrid_reranked"][f"recall@{K}"] >= 0.6, report


@pytest.mark.llm
@pytest.mark.golden
def test_vector_leg_on_real_embeddings(latest, reports_dir):
    base, key = os.getenv("LLM_EVAL_BASE_URL"), os.getenv("LLM_EVAL_API_KEY")
    if not (base and key):
        pytest.skip("set LLM_EVAL_BASE_URL / LLM_EVAL_API_KEY (or FANVAAZ_LLM_* in .env) for embeddings")
    from fanvaaz.config import Settings
    from fanvaaz.llm import OpenAICompatLLM
    from fanvaaz.store import Store

    s = Settings(llm_base_url=base, llm_api_key=key, llm_enabled=True)
    cache = Store(reports_dir / "embed_cache.db")  # content-hash cache: re-runs embed nothing new
    embedder = Embedder(OpenAICompatLLM(s), s, cache)

    chunks, _ = chunks_from_videos(latest["videos"])
    vecs = embedder.embed([c.text for c in chunks], node="eval.embed")
    assert vecs, "embedding call failed; see the log for the provider's message"
    vectors = {c.doc_id: v for c, v in zip(chunks, vecs, strict=True)}
    retriever = HybridRetriever(chunks, vectors, embedder)
    assert retriever.has_vectors

    sentiment_of = {(m.source_id, m.brand): m.sentiment for m in latest["mentions"] if m.source == "comment"}
    rows = _rows()
    relevant = [set(r["relevant"]) for r in rows]
    report = {
        "questions": len(rows),
        "k": K,
        "embedding_model": s.embed_model,
        "dims": s.embed_dims,
        "chunks": len(chunks),
        "note": "pooled judgments predate the vector leg: vector-only scores are a lower bound",
        "bm25": _metrics(_simple(retriever, rows, "lexical"), relevant),
        "vector_only": _metrics(_simple(retriever, rows, "vector"), relevant),
        "hybrid_rrf_reranked": _metrics(_full(retriever, rows, sentiment_of), relevant),
    }
    (reports_dir / "retrieval_vectors.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    assert report["vector_only"]["mrr"] > 0, "vector search found nothing relevant: check the embeddings"
    assert report["hybrid_rrf_reranked"]["mrr"] >= report["bm25"]["mrr"], report
    assert report["hybrid_rrf_reranked"][f"recall@{K}"] >= 0.6, report
