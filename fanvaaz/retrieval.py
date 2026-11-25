"""Hybrid retrieval over one run's comments: search two ways, fuse, re-rank.

    lexical  BM25 over comment tokens (always available, no key needed)
    vector   cosine over Gemini embeddings: pgvector HNSW on Postgres,
             an in-memory matrix on SQLite (one run is a few thousand comments)
    fuse     Reciprocal Rank Fusion (k=60): robust to the two scores' scales
    rerank   features a marketer cares about: brand match, aspect match,
             comment likes (log), the video's search rank; optionally the
             Vertex AI Ranking API as a cross-encoder second stage

Every hit keeps its rank in each leg, so evaluation (recall@k, MRR per mode)
and the evidence panel can show why a comment was chosen.

Comments that look like prompt injections are never indexed: they still count
toward Share of Voice, but cannot reach a model as context.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from . import obs, tokens
from .brands import brands_in
from .config import Settings, get_settings
from .governance import Governance
from .guardrails import is_injection
from .models import Video

log = logging.getLogger("fanvaaz.retrieval")

TOKEN = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+")
STOP = {
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
    "vs",
    "which",
    "why",
    "me",
    "tell",
    "i",
}
RRF_K = 60


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass
class Chunk:
    doc_id: str  # comment id
    video_id: str
    keyword: str
    text: str
    brands: list[str]
    like_count: int = 0
    rank: int = 50
    tokens: list[str] = field(default_factory=list)


@dataclass
class Hit:
    chunk: Chunk
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None
    rrf: float = 0.0
    features: dict = field(default_factory=dict)


def chunks_from_videos(videos: list[Video]) -> tuple[list[Chunk], int]:
    """(indexable chunks, number quarantined as injections). Duplicate comment ids are indexed once."""
    out, seen, quarantined = [], set(), 0
    for v in videos:
        for c in v.comments:
            if c.comment_id in seen:
                continue
            seen.add(c.comment_id)
            if is_injection(c.text):
                quarantined += 1
                continue
            out.append(
                Chunk(c.comment_id, v.video_id, v.keyword, c.text, brands_in(c.text), c.like_count, v.rank)
            )
    return out, quarantined


class BM25:
    def __init__(self, docs: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        for d in docs:
            d.tokens = tokenize(d.text)
        self.avgdl = sum(len(d.tokens) for d in docs) / max(len(docs), 1)
        df: Counter = Counter()
        for d in docs:
            df.update(set(d.tokens))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.tf = [Counter(d.tokens) for d in docs]

    def search(self, query: str, k: int = 50, must_brand: str | None = None) -> list[tuple[float, Chunk]]:
        q = tokenize(query)
        scored = []
        for i, d in enumerate(self.docs):
            if must_brand and must_brand not in d.brands:
                continue
            s = 0.0
            for t in q:
                f = self.tf[i].get(t)
                if f:
                    s += (
                        self.idf[t]
                        * f
                        * (self.k1 + 1)
                        / (f + self.k1 * (1 - self.b + self.b * len(d.tokens) / self.avgdl))
                    )
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:k]


# ---- embeddings ------------------------------------------------------------------------
class Embedder:
    """Embeddings through the gateway's governance, with a content-hash cache in the DB."""

    def __init__(self, client, settings: Settings | None = None, store=None):
        self.client, self.s, self.store = client, settings or get_settings(), store
        self.model = self.s.embed_model

    def available(self) -> bool:
        gov = Governance(self.s, self.store)
        if getattr(self.client, "circuit_open", None):
            return False
        return self.s.llm_enabled and gov.model_allowed(self.model, "embeddings")

    def embed(self, texts: list[str], node: str = "rag.embed") -> list[list[float]] | None:
        if not texts or not self.available():
            return None
        hashes = [content_hash(t) for t in texts]
        cached: dict[str, list[float]] = {}
        if self.store is not None:
            with self.store._conn() as c:
                for i in range(0, len(hashes), 500):
                    part = hashes[i : i + 500]
                    marks = ",".join("?" * len(part))
                    for h, e in c.execute(
                        f"SELECT content_hash, embedding FROM embedding_cache WHERE model = ? AND content_hash IN ({marks})",
                        (self.model, *part),
                    ).fetchall():
                        cached[h] = json.loads(e)
        todo = [(h, t) for h, t in zip(hashes, texts, strict=True) if h not in cached]
        todo = list(dict(todo).items())
        if todo:
            est = sum(tokens.estimate(t) for _, t in todo)
            rec = obs.CallRecord(
                node=node,
                model=self.model,
                prompt_id="embed",
                prompt_version="-",
                est_prompt_tokens=est,
                prompt_tokens=est,
                cost_usd=est * 0.15 / 1_000_000,
            )
            try:
                vecs = self.client.embed([t for _, t in todo], model=self.model, dims=self.s.embed_dims)
            except Exception as e:  # noqa: BLE001 - no vectors means lexical-only retrieval
                log.warning("embedding failed, lexical retrieval only: %s", e)
                rec.outcome, rec.prompt_tokens, rec.cost_usd = "error", 0, 0.0
                obs.record_call(rec)
                return None
            obs.record_call(rec)
            new = {h: v for (h, _), v in zip(todo, vecs, strict=True)}
            cached.update(new)
            if self.store is not None:
                with self.store._conn() as c:
                    c.executemany(
                        "INSERT INTO embedding_cache (content_hash, model, embedding) VALUES (?,?,?)"
                        " ON CONFLICT (content_hash, model) DO NOTHING",
                        [(h, self.model, json.dumps(v)) for h, v in new.items()],
                    )
        return [cached[h] for h in hashes]


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ---- indexing -------------------------------------------------------------------------
def index_run(store, run_id: str, videos: list[Video], embedder: Embedder | None = None) -> dict:
    """Write the run's comments as RAG chunks (with embeddings when an embedder is available)."""
    chunks, quarantined = chunks_from_videos(videos)
    vectors = embedder.embed([c.text for c in chunks]) if embedder and chunks else None
    rows = []
    for i, ch in enumerate(chunks):
        emb = None
        if vectors is not None:
            emb = vec_literal(vectors[i]) if store.dialect == "postgres" else json.dumps(vectors[i])
        rows.append(
            (
                run_id,
                ch.doc_id,
                ch.video_id,
                ch.keyword,
                ch.text,
                json.dumps(ch.brands),
                ch.like_count,
                ch.rank,
                None,
                content_hash(ch.text),
                emb,
            )
        )
    sql = (
        "INSERT INTO rag_chunks (run_id, comment_id, video_id, keyword, text, brands, like_count, rank,"
        " published_at, content_hash, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,"
        + ("?::vector" if store.dialect == "postgres" else "?")
        + ") ON CONFLICT (run_id, comment_id) DO UPDATE SET embedding = excluded.embedding"
    )
    with store._conn() as c:
        c.executemany(sql, rows)
    return {"chunks": len(chunks), "quarantined": quarantined, "embedded": vectors is not None}


def load_chunks(store, run_id: str) -> tuple[list[Chunk], dict[str, list[float]]]:
    """The run's chunks and, on SQLite, their vectors (Postgres searches vectors in SQL)."""
    with store._conn() as c:
        rows = c.execute(
            "SELECT comment_id, video_id, keyword, text, brands, like_count, rank,"
            + (" embedding IS NOT NULL" if store.dialect == "postgres" else " embedding")
            + " FROM rag_chunks WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    chunks, vecs = [], {}
    for r in rows:
        chunks.append(Chunk(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6]))
        if store.dialect == "sqlite" and r[7]:
            vecs[r[0]] = json.loads(r[7])
        elif store.dialect == "postgres" and r[7]:
            vecs[r[0]] = []  # marker: vectors exist, searched in SQL
    return chunks, vecs


# ---- search ---------------------------------------------------------------------------
class HybridRetriever:
    def __init__(
        self,
        chunks: list[Chunk],
        vectors: dict[str, list[float]] | None = None,
        embedder: Embedder | None = None,
        store=None,
        run_id: str | None = None,
    ):
        self.chunks = chunks
        self.by_id = {c.doc_id: c for c in chunks}
        self.bm25 = BM25(chunks)
        self.vectors = vectors or {}
        self.embedder, self.store, self.run_id = embedder, store, run_id
        self._matrix = None
        if (
            self.vectors
            and store is not None
            and store.dialect == "sqlite"
            or (self.vectors and store is None)
        ):
            import numpy as np

            ids = [cid for cid in self.vectors if cid in self.by_id and self.vectors[cid]]
            if ids:
                self._ids = ids
                self._matrix = np.array([self.vectors[i] for i in ids], dtype="float32")

    @property
    def has_vectors(self) -> bool:
        return bool(self.vectors) and self.embedder is not None

    def _vector_search(self, query_vec: list[float], k: int, must_brand: str | None) -> list[str]:
        if self.store is not None and self.store.dialect == "postgres":
            sql = "SELECT comment_id, brands FROM rag_chunks WHERE run_id = ? AND embedding IS NOT NULL"
            sql += " ORDER BY embedding <=> ?::vector LIMIT ?"
            with self.store._conn() as c:
                rows = c.execute(sql, (self.run_id, vec_literal(query_vec), k * 3)).fetchall()
            ids = [r[0] for r in rows if not must_brand or must_brand in json.loads(r[1])]
            return ids[:k]
        if self._matrix is None:
            return []
        import numpy as np

        sims = self._matrix @ np.array(query_vec, dtype="float32")
        order = np.argsort(-sims)
        out = []
        for i in order:
            cid = self._ids[int(i)]
            if must_brand and must_brand not in self.by_id[cid].brands:
                continue
            out.append(cid)
            if len(out) == k:
                break
        return out

    def search(
        self,
        query: str,
        k: int = 6,
        must_brand: str | None = None,
        aspect_words: list[str] | None = None,
        mode: str = "hybrid",
        query_vec: list[float] | None = None,
        want_sentiment: str | None = None,
        sentiment_of: dict[tuple[str, str], str] | None = None,
    ) -> list[Hit]:
        """mode: lexical | vector | hybrid (fused + re-ranked).

        want_sentiment ("positive" for praise questions, "negative" for complaints) adds the
        brand's comments with that sentiment as extra candidates and as a re-rank feature:
        on real data a praise question otherwise retrieves "Samsung in reels" memes.
        """
        depth = max(50, k * 8)
        lexical = (
            [] if mode == "vector" else [c.doc_id for _, c in self.bm25.search(query, depth, must_brand)]
        )
        vector: list[str] = []
        if mode != "lexical" and self.has_vectors:
            if query_vec is None:
                vecs = self.embedder.embed([query], node="ask.embed_query")
                query_vec = vecs[0] if vecs else None
            if query_vec is not None:
                vector = self._vector_search(query_vec, depth, must_brand)
        hits: dict[str, Hit] = {}
        for rank, cid in enumerate(lexical, 1):
            h = hits.setdefault(cid, Hit(self.by_id[cid], 0.0))
            h.lexical_rank = rank
            h.rrf += 1 / (RRF_K + rank)
        for rank, cid in enumerate(vector, 1):
            if cid not in self.by_id:
                continue
            h = hits.setdefault(cid, Hit(self.by_id[cid], 0.0))
            h.vector_rank = rank
            h.rrf += 1 / (RRF_K + rank)
        if want_sentiment and must_brand and sentiment_of and mode == "hybrid":
            pool = [
                c
                for c in self.chunks
                if must_brand in c.brands and sentiment_of.get((c.doc_id, must_brand)) == want_sentiment
            ]
            pool.sort(key=lambda c: c.like_count, reverse=True)
            for i, c in enumerate(pool[:depth]):
                h = hits.setdefault(c.doc_id, Hit(c, 0.0))
                h.rrf += 1 / (RRF_K + depth + i)
        if not hits:
            return []
        if mode in ("lexical", "vector"):
            ranked = sorted(hits.values(), key=lambda h: h.rrf, reverse=True)
            for h in ranked:
                h.score = h.rrf
            return ranked[:k]
        return self.rerank(
            list(hits.values()), query, k, must_brand, aspect_words or [], want_sentiment, sentiment_of or {}
        )

    def rerank(
        self,
        hits: list[Hit],
        query: str,
        k: int,
        brand: str | None,
        aspect_words: list[str],
        want_sentiment: str | None = None,
        sentiment_of: dict[tuple[str, str], str] | None = None,
    ) -> list[Hit]:
        sentiment_of = sentiment_of or {}
        top_rrf = max(h.rrf for h in hits) or 1.0
        max_likes = max((h.chunk.like_count for h in hits), default=0)
        for h in hits:
            low = h.chunk.text.lower()
            f = {
                "rrf": h.rrf / top_rrf,
                "brand": 1.0 if brand and brand in h.chunk.brands else 0.0,
                "aspect": 1.0
                if any(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", low) for w in aspect_words)
                else 0.0,
                "likes": math.log1p(h.chunk.like_count) / math.log1p(max_likes) if max_likes else 0.0,
                "video_rank": 1 / math.log2(h.chunk.rank + 1),
                "both_legs": 1.0 if h.lexical_rank and h.vector_rank else 0.0,
                # one-word and pure-emoji comments explain nothing
                "substance": min(len(tokenize(h.chunk.text)) / 12, 1.0),
                "sentiment": 1.0
                if want_sentiment and brand and sentiment_of.get((h.chunk.doc_id, brand)) == want_sentiment
                else 0.0,
            }
            h.features = {k_: round(v, 3) for k_, v in f.items()}
            h.score = (
                0.40 * f["rrf"]
                + 0.10 * f["brand"]
                + 0.10 * f["aspect"]
                + 0.15 * f["sentiment"]
                + 0.10 * f["substance"]
                + 0.07 * f["likes"]
                + 0.05 * f["video_rank"]
                + 0.03 * f["both_legs"]
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        out, seen = [], set()
        for h in hits:  # near-duplicate comments ("same here", copy-paste) add nothing
            key = " ".join(h.chunk.text.lower().split())[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
            if len(out) == k:
                break
        return vertex_rerank(query, out) if get_settings().vertex_rerank else out


def vertex_rerank(query: str, hits: list[Hit]) -> list[Hit]:
    """Optional cross-encoder stage: Vertex AI Ranking API (semantic-ranker). Falls back silently."""
    s = get_settings()
    if not (s.gcp_project and hits):
        return hits
    try:
        import google.auth
        import google.auth.transport.requests
        import httpx

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        url = (
            f"https://discoveryengine.googleapis.com/v1/projects/{s.gcp_project}/locations/global/"
            "rankingConfigs/default_ranking_config:rank"
        )
        body = {
            "model": "semantic-ranker-default@latest",
            "query": query,
            "records": [{"id": h.chunk.doc_id, "content": h.chunk.text} for h in hits],
        }
        r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {creds.token}"}, timeout=10)
        r.raise_for_status()
        order = {rec["id"]: rec.get("score", 0) for rec in r.json().get("records", [])}
        for h in hits:
            h.features["vertex"] = round(order.get(h.chunk.doc_id, 0), 3)
        return sorted(hits, key=lambda h: order.get(h.chunk.doc_id, 0), reverse=True)
    except Exception as e:  # noqa: BLE001
        log.warning("Vertex rerank skipped: %s", e)
        return hits


# ---- evaluation -----------------------------------------------------------------------
def evaluate(retriever: HybridRetriever, labelled: list[dict], k: int = 5) -> dict:
    """recall@k and MRR per mode on questions labelled with relevant comment ids."""
    out = {}
    for mode in ("lexical", "vector", "hybrid"):
        if mode != "lexical" and not retriever.has_vectors:
            continue
        recalls, rrs = [], []
        for item in labelled:
            relevant = set(item["relevant"])
            got = [
                h.chunk.doc_id
                for h in retriever.search(
                    item["question"],
                    k=k,
                    must_brand=item.get("brand"),
                    aspect_words=item.get("aspect_words"),
                    mode=mode,
                )
            ]
            recalls.append(len(relevant & set(got)) / len(relevant) if relevant else 0.0)
            rr = next((1 / i for i, cid in enumerate(got, 1) if cid in relevant), 0.0)
            rrs.append(rr)
        n = max(len(labelled), 1)
        out[mode] = {
            f"recall@{k}": round(sum(recalls) / n, 3),
            "mrr": round(sum(rrs) / n, 3),
            "n": len(labelled),
        }
    return out
