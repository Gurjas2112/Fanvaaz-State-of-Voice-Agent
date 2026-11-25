"""Ranking quality on real comments: does the retriever put the right comments first?

ask_real.jsonl: 8 real questions over the recorded YouTube snapshot, each with the comment
ids a human judged relevant. Judgments were pooled (the top candidates of the lexical and
the re-ranked hybrid retriever, judged by hand), the standard IR practice; comments neither
system retrieved count as not relevant, so absolute recall is an upper bound.

Compared: plain BM25 vs the full pipeline (brand-free query, intent-aware candidates,
feature re-rank). The vector leg joins when an embedding key is configured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fanvaaz.ask import Answerer, aspect_words
from fanvaaz.graph.ask_graph import intent_of, without_brands

GOLDEN = Path(__file__).parent / "ask_real.jsonl"
K = 5


def _metrics(ranked: list[list[str]], relevant: list[set[str]]) -> dict:
    recalls, rrs, precisions = [], [], []
    for got, rel in zip(ranked, relevant, strict=True):
        top = got[:K]
        recalls.append(len(rel & set(top)) / min(len(rel), K))
        precisions.append(len(rel & set(top)) / K)
        rrs.append(next((1 / i for i, cid in enumerate(top, 1) if cid in rel), 0.0))
    n = len(ranked)
    return {f"recall@{K}": round(sum(recalls) / n, 3), f"precision@{K}": round(sum(precisions) / n, 3),
            "mrr": round(sum(rrs) / n, 3)}


@pytest.mark.golden
def test_reranked_retrieval_beats_plain_bm25(latest, reports_dir):
    a = Answerer(latest["videos"], latest["mentions"], latest["reports"])
    rows = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    relevant = [set(r["relevant"]) for r in rows]
    plain, full = [], []
    for r in rows:
        words = aspect_words(r["aspect"])
        plain.append([h.chunk.doc_id for h in a.retriever.search(r["question"], k=K, must_brand=r["brand"],
                                                                  mode="lexical")])
        query = without_brands(r["question"]) + " " + " ".join(words[:4])
        full.append([h.chunk.doc_id for h in a.retriever.search(
            query, k=K, must_brand=r["brand"], aspect_words=words,
            want_sentiment=intent_of(r["question"]), sentiment_of=a.sentiment_of)])
    report = {"questions": len(rows), "k": K, "bm25": _metrics(plain, relevant),
              "hybrid_reranked": _metrics(full, relevant)}
    (reports_dir / "retrieval.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert report["hybrid_reranked"]["mrr"] >= report["bm25"]["mrr"]
    assert report["hybrid_reranked"][f"precision@{K}"] >= report["bm25"][f"precision@{K}"]
    assert report["hybrid_reranked"][f"recall@{K}"] >= 0.6, report
