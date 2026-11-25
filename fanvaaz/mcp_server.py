"""Fanvaaz as an MCP server: Share of Voice, insights, risks and grounded Q&A as tools.

Transports
  stdio            `fanvaaz mcp` (Claude Desktop / Claude Code on the same machine)
  streamable HTTP  mounted at /mcp on the API (Cloud Run), Bearer token required

Scopes (least privilege)
  read   FANVAAZ_MCP_TOKEN: every read tool, plus 1-unit YouTube lookups
  admin  FANVAAZ_API_TOKEN, or local stdio: also `run_collection` and `youtube_search`
         (100 quota units per call)

The YouTube tools are inspired by temiedani/youtube-mcp-server (video info,
comments, search), rebuilt on Fanvaaz's collector: API-key auth instead of an
OAuth token file, quota accounting, PII redaction at ingestion, and logs on
stderr only (stdout carries the MCP JSON-RPC stream).
"""

from __future__ import annotations

import contextvars
import hmac
import json
import logging
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import obs
from . import profile as profile_mod
from .config import get_settings
from .evidence import pack as evidence_pack
from .service import answerer_for, clear_answerers, default_store

log = logging.getLogger("fanvaaz.mcp")

_scope: contextvars.ContextVar[str] = contextvars.ContextVar("fanvaaz_mcp_scope", default="admin")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

mcp = FastMCP(
    "fanvaaz",
    instructions=(
        "Fanvaaz measures YouTube Share of Voice for a tracked brand against its rivals. "
        "Use get_share_of_voice and get_insights for numbers, search_comments for raw evidence, "
        "and ask_fanvaaz for a grounded answer with citations. Numbers come only from the latest run."
    ),
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    # Auth is the Bearer token checked by `authorize` below; Host checks would reject Cloud Run's host.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _require_admin(tool: str) -> None:
    if _scope.get() != "admin":
        raise PermissionError(f"{tool} needs the admin token (FANVAAZ_API_TOKEN)")


def _latest():
    store = default_store()
    info = store.latest_run()
    if info is None:
        raise ValueError("No data yet: run a collection first.")
    return store, info


@mcp.tool()
def list_keywords() -> list[str]:
    """Keywords in the latest run ('all' is the combined view)."""
    store, info = _latest()
    return ["all"] + [k for k in store.keywords(info.run_id) if k != "all"]


@mcp.tool()
def get_share_of_voice(keyword: str = "all") -> dict:
    """Share of Voice per brand for one keyword: sov, mention/engagement/positive share, net sentiment."""
    store, info = _latest()
    rep = store.report(info.run_id, keyword[:100])
    if rep is None:
        raise ValueError(f"unknown keyword '{keyword}'")
    return {
        "run_id": info.run_id,
        "focus_brand": info.focus_brand,
        "collected_at": info.created_at,
        "keyword": keyword,
        "brands": [b.model_dump() for b in rep.brands],
    }


@mcp.tool()
def get_insights() -> list[dict]:
    """Insights and recommendations for the focus brand, each with evidence numbers and citations."""
    store, info = _latest()
    return [
        {k: i.get(k) for k in ("id", "kind", "title", "text", "evidence", "citations", "verified")}
        for i in store.insights(info.run_id)
    ]


@mcp.tool()
def get_risks(brand: str | None = None, severity: str | None = None, limit: int = 20) -> list[dict]:
    """Comments needing escalation (safety, legal/consumer, product or service failure), highest score first."""
    if severity not in (None, "high", "medium", "low"):
        raise ValueError("severity must be high, medium or low")
    store, info = _latest()
    return store.risks(info.run_id, brand=brand, severity=severity, limit=max(1, min(limit, 100)))


@mcp.tool()
def search_comments(query: str, brand: str | None = None, k: int = 8) -> list[dict]:
    """Hybrid search (BM25 + embeddings, re-ranked) over the latest run's PII-redacted comments."""
    store, info = _latest()
    a = answerer_for(info, store)
    hits = a.retriever.search(query[:500], k=max(1, min(k, 20)), must_brand=brand)
    return [
        {
            "comment_id": h.chunk.doc_id,
            "video_id": h.chunk.video_id,
            "keyword": h.chunk.keyword,
            "text": h.chunk.text,
            "score": round(h.score, 4),
            "features": h.features,
        }
        for h in hits
    ]


@mcp.tool()
def ask_fanvaaz(question: str, thread_id: str | None = None) -> dict:
    """A grounded answer with citations and reasoning steps. Refuses injections and off-topic questions."""
    store, info = _latest()
    out = answerer_for(info, store).answer(
        question[:1000], user_key="mcp", thread_id=(thread_id or "")[:32] or None
    )
    return {
        k: out.get(k)
        for k in ("answer", "mode", "refused", "reasons", "citations", "sources", "reasoning_steps")
    }


@mcp.tool()
def get_evidence() -> dict:
    """The latest run's evidence pack: method, every insight with numbers, citations and verification."""
    store, info = _latest()
    return evidence_pack(
        info, store.insights(info.run_id), store.risks(info.run_id, limit=20), get_settings()
    )


@mcp.tool()
def get_ai_ops() -> dict:
    """LLM usage for the last 7 days: calls, tokens (estimated vs actual), cache hits, cost, latency."""
    ops = obs.ai_ops(default_store())
    ops.pop("recent", None)
    return ops


def _collector():
    from .collector import YouTubeCollector

    s = get_settings().model_copy(update={"save_raw": False})
    return YouTubeCollector(s)


@mcp.tool()
def youtube_video_info(video_id: str) -> dict:
    """Title, channel and statistics for one YouTube video (1 quota unit)."""
    if not VIDEO_ID.match(video_id):
        raise ValueError("video_id must be an 11-character YouTube id")
    col = _collector()
    vids = col.videos([video_id], keyword="mcp")
    if not vids:
        raise ValueError("video not found")
    v = vids[0]
    return {**v.model_dump(exclude={"comments", "rank", "keyword"}), "quota_units": col.units}


@mcp.tool()
def youtube_video_comments(video_id: str, max_results: int = 20) -> dict:
    """Top comments on one video, PII-redacted, with brands detected (1 quota unit)."""
    if not VIDEO_ID.match(video_id):
        raise ValueError("video_id must be an 11-character YouTube id")
    from .brands import brands_in

    col = _collector()
    col.s = col.s.model_copy(update={"comments_per_video": max(1, min(max_results, 100))})
    return {
        "comments": [
            {"text": c.text, "likes": c.like_count, "brands": brands_in(c.text)}
            for c in col.comments(video_id)
        ],
        "quota_units": col.units,
    }


@mcp.tool()
def youtube_search(query: str, max_results: int = 10) -> list[dict]:
    """Search YouTube videos (admin scope: 100 quota units per call)."""
    _require_admin("youtube_search")
    col = _collector()
    col.s = col.s.model_copy(update={"top_n": max(1, min(max_results, 50))})
    ids = col.search(query[:100])
    return [v.model_dump(exclude={"comments", "keyword"}) for v in col.videos(ids, keyword=query[:100])]


@mcp.tool()
def run_collection(keywords: list[str] | None = None) -> dict:
    """Collect live YouTube data now and score it (admin scope; about 151 quota units per keyword)."""
    _require_admin("run_collection")
    from .pipeline import run

    info = run(
        source="youtube", keywords=keywords[:10] if keywords else None, store=default_store(), actor="mcp"
    )
    clear_answerers()
    return info.model_dump()


@mcp.resource("fanvaaz://profile")
def profile_resource() -> str:
    """What is tracked: focus brand, rivals, aliases, keywords, aspects."""
    return profile_mod.active().model_dump_json(indent=2)


@mcp.resource("fanvaaz://policy")
def policy_resource() -> str:
    """The AI governance policy in force."""
    from .governance import Governance

    return json.dumps(Governance(get_settings(), default_store()).summary(), indent=2)


@mcp.prompt()
def weekly_brief(audience: str = "marketing team") -> str:
    """A prompt that asks the client model to write a brief from Fanvaaz tools only."""
    return (
        f"Write a short Share of Voice brief for the {audience}. Call get_share_of_voice, get_insights and "
        "get_risks first. Use only numbers those tools return, and cite comment or video ids for every claim."
    )


# ---- HTTP mounting with Bearer auth ---------------------------------------------------
def http_app():
    """The streamable-HTTP MCP app wrapped in Bearer-token auth (mounted at /mcp by the API)."""
    inner = mcp.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        token = auth[7:] if auth.startswith("Bearer ") else ""
        s = get_settings()
        level = None
        if token and s.api_token and hmac.compare_digest(token, s.api_token):
            level = "admin"
        elif token and s.mcp_token and hmac.compare_digest(token, s.mcp_token):
            level = "read"
        if level is None:
            body = b'{"error":"MCP needs Authorization: Bearer <FANVAAZ_MCP_TOKEN or FANVAAZ_API_TOKEN>"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        tok = _scope.set(level)
        try:
            await inner(scope, receive, send)
        finally:
            _scope.reset(tok)

    return app


def main() -> None:
    """stdio transport for local MCP clients; logs go to stderr."""
    obs.configure_logging(json_logs=False)
    default_store()
    try:
        profile_mod.sync_from(default_store())
    except profile_mod.NoProfileError:
        log.warning("no tracking profile yet; tools will report that until an admin picks one")
    mcp.run(transport="stdio")
