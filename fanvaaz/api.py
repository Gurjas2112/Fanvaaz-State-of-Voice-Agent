"""FastAPI service: pages, auth, JSON API, MCP and ops endpoints.

Pages   /  /login  /signup  /app (signed in)  /admin (admins)
API     /api/v1/...  (signed-in session cookie, or `Authorization: Bearer <FANVAAZ_API_TOKEN>`)
MCP     /mcp (streamable HTTP, Bearer FANVAAZ_MCP_TOKEN or FANVAAZ_API_TOKEN)
Ops     /health (liveness)  /ready (dependencies)  /metrics (Prometheus, admin)
Docs    /docs (OpenAPI; admin-only when FANVAAZ_PUBLIC_DOCS=false). Postman, Schemathesis,
        Playwright, Appium and promptfoo run against this app.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path as FilePath
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__, obs, prompts
from . import profile as profile_mod
from .auth import COOKIE, CSRF_HEADER, CSRF_VALUE, EMAIL_PATTERN, Accounts, AuthError, User
from .brief import build_brief
from .evidence import pack as evidence_pack
from .governance import Governance, audit, audit_log
from .memory import Memory, user_key
from .models import KeywordReport, RunInfo
from .pipeline import new_run_id
from .pipeline import run as run_pipeline
from .risk import RISK_CATEGORIES, summarize
from .service import answerer_for, clear_answerers, default_store
from .store import Store

log = logging.getLogger("fanvaaz.api")
STATIC = FilePath(__file__).parent / "static"


def get_settings():  # re-exported so tests and ops code can patch one place
    from .config import get_settings as _gs

    return _gs()


def get_store() -> Store:
    return default_store()


_accounts: dict[int, Accounts] = {}


def get_accounts(store: Store = Depends(get_store)) -> Accounts:
    key = id(store)
    if key not in _accounts:
        _accounts[key] = Accounts(store, get_settings())
    return _accounts[key]


def _mcp_app():
    try:
        from .mcp_server import http_app, mcp

        return http_app(), mcp
    except Exception:  # noqa: BLE001 - the API must start even if the MCP SDK is missing
        log.exception("MCP server disabled")
        return None, None


MCP_APP, MCP = _mcp_app()
_mcp_started = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    obs.configure_logging(json_logs=s.log_json)
    store = app.dependency_overrides.get(get_store, get_store)()
    obs.bind(store)
    get_accounts(store).ensure_admin()
    try:
        profile_mod.sync_from(store)
    except profile_mod.NoProfileError:
        log.warning("no tracking profile yet: an admin must choose one on /admin")
    task = None
    if s.daily_run_at:
        from .scheduler import daily_loop

        task = asyncio.create_task(daily_loop(s, store))
    global _mcp_started
    if MCP is not None and not _mcp_started:
        # The MCP session manager can run once per process (tests open several app clients).
        _mcp_started = True
        async with MCP.session_manager.run():
            yield
    else:
        yield
    if task:
        task.cancel()


app = FastAPI(
    title="Fanvaaz API",
    version=__version__,
    description=(
        "Daily YouTube Share of Voice for any brand: LangGraph collection and Ask agents, hybrid RAG, "
        "risk inbox, evidence packs, AI governance and observability."
    ),
    lifespan=lifespan,
)

CSP = (
    "default-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' data: https://i.ytimg.com; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'; manifest-src 'self'; worker-src 'self'"
)
DOC_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")


def _allowed_methods(path: str) -> list[str]:
    methods: set[str] = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path_regex.match(path):
            methods |= route.methods
    return sorted(methods)


def _request_user(request: Request) -> User | None:
    store = app.dependency_overrides.get(get_store, get_store)()
    acc = get_accounts(store)
    return acc.user_for_bearer(request.headers.get("authorization")) or acc.user_for_token(
        request.cookies.get(COOKIE)
    )


@app.middleware("http")
async def headers(request: Request, call_next):
    t0 = time.perf_counter()
    if request.url.path in DOC_PATHS and not get_settings().public_docs:
        user = await asyncio.to_thread(_request_user, request)
        if user is None or user.role != "admin":
            return JSONResponse(
                status_code=401, content={"detail": "API docs are for admins. Sign in first."}
            )
    response = await call_next(request)
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if response.status_code == 405:
        # Starlette lists only the first matching route's methods; GET and PUT on
        # /api/v1/admin/profile are two routes, so build Allow from all of them.
        response.headers["Allow"] = ", ".join(_allowed_methods(request.url.path))
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi", "/mcp")):
        response.headers["Content-Security-Policy"] = CSP
    return response


@app.exception_handler(AuthError)
async def auth_error(request: Request, exc: AuthError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


@app.exception_handler(profile_mod.NoProfileError)
async def no_profile(request: Request, exc: profile_mod.NoProfileError):
    return JSONResponse(status_code=409, content={"detail": str(exc), "setup_required": True})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error"})


# ---- auth dependencies -------------------------------------------------------
def optional_user(request: Request, accounts: Accounts = Depends(get_accounts)) -> User | None:
    return accounts.user_for_bearer(request.headers.get("authorization")) or accounts.user_for_token(
        request.cookies.get(COOKIE)
    )


def current_user(request: Request, user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    # CSRF: cookie-authenticated writes must carry the custom header (Bearer clients are exempt).
    if request.method in {"POST", "PATCH", "PUT", "DELETE"} and not request.headers.get("authorization"):
        if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
            raise HTTPException(403, "Missing X-Requested-With header.")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admins only.")
    return user


def csrf_for_anonymous(request: Request) -> None:
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise HTTPException(403, "Missing X-Requested-With header.")


def latest_or_404(store: Store = Depends(get_store), _: User = Depends(current_user)) -> RunInfo:
    info = store.latest_run()
    if not info:
        raise HTTPException(404, "No data yet. An admin can start a live collection from the admin page.")
    return info


# Abuse control on the one endpoint that can spend LLM tokens: a per-user sliding window.
_ask_hits: dict[int | str, deque] = {}
_ask_lock = threading.Lock()


def ask_rate_limit(user: User = Depends(current_user)) -> User:
    limit = get_settings().ask_rate_per_min
    now = time.monotonic()
    with _ask_lock:
        q = _ask_hits.setdefault(user.id, deque())
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, f"Too many questions: the limit is {limit} a minute.")
        q.append(now)
    return user


# ---- schemas -----------------------------------------------------------------
class StrictIn(BaseModel):
    """Request bodies: no type coercion ("active": 0 is not false) and no unknown fields."""

    model_config = ConfigDict(strict=True, extra="forbid")


class ErrorBody(BaseModel):
    detail: str


AUTH = {401: {"model": ErrorBody, "description": "Not signed in"}}
NO_DATA = {**AUTH, 404: {"model": ErrorBody, "description": "No runs yet, or unknown keyword"}}
WRITE = {
    400: {
        "model": ErrorBody,
        "description": "Body is not valid JSON, or a value breaks a rule (e.g. weak password)",
    },
    403: {"model": ErrorBody, "description": "Missing CSRF header or not allowed"},
}
ADMIN = {**AUTH, 403: {"model": ErrorBody, "description": "Admins only"}}


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    has_data: bool
    llm_configured: bool
    profile_configured: bool


class Ready(BaseModel):
    ready: bool
    checks: dict[str, Any]


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: Literal["admin", "analyst"]
    active: bool
    created_at: str
    last_login: str | None


class SignupIn(StrictIn):
    name: str = Field(min_length=1, max_length=80, pattern=r"\S")
    email: str = Field(min_length=6, max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=10, max_length=200)


class LoginIn(StrictIn):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class UserPatch(StrictIn):
    role: Literal["admin", "analyst"] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


class SovResponse(BaseModel):
    run: RunInfo
    focus_brand: str
    report: KeywordReport


THREAD = r"^[a-f0-9]{8,32}$"


class AskRequest(StrictIn):
    question: str = Field(
        min_length=1, max_length=1000, examples=["Why do people complain about the battery?"]
    )
    thread_id: str | None = Field(default=None, pattern=THREAD, description="Continue a conversation")


class Source(BaseModel):
    comment_id: str
    video_id: str
    keyword: str
    text: str
    citation: str | None = None
    score: float | None = None
    lexical_rank: int | None = None
    vector_rank: int | None = None
    features: dict[str, float] = Field(default_factory=dict)
    for_brand: str | None = None


class AskResponse(BaseModel):
    answer: str
    refused: bool
    reasons: list[str]
    mode: Literal["guard", "extractive", "llm"]
    sources: list[Source]
    citations: list[str] = Field(default_factory=list)
    scope: dict[str, Any] | None = None
    reasoning_steps: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    cache: dict[str, Any] = Field(default_factory=dict)
    risk_score: float = 0.0
    rejected_llm_answer: bool = False
    fallback_reason: str | None = None
    thread_id: str | None = None
    trace_id: str | None = None


class RunRequest(StrictIn):
    source: Literal["youtube"] = "youtube"
    keywords: list[str] | None = Field(default=None, max_length=10)


class JobOut(BaseModel):
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    started_at: str
    finished_at: str | None = None
    run_id: str | None = None
    error: str | None = None
    progress: str | None = None


class ProfileSummary(BaseModel):
    id: str
    name: str
    category: str
    focus_brand: str
    rivals: list[str]
    keywords: list[str]


PresetId = Literal[tuple(profile_mod.presets())]  # type: ignore[valid-type]


class ProfilePut(StrictIn):
    """Either switch to a template, or save a full custom profile."""

    preset: PresetId | None = None
    profile: profile_mod.Profile | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ProfilePut:
        if (self.preset is None) == (self.profile is None):
            raise ValueError("send either a preset id or a full profile, not both")
        return self


class ProfileAdmin(BaseModel):
    active: profile_mod.Profile | None
    updated_by: str | None
    updated_at: str | None
    presets: list[ProfileSummary]


class PrefsIn(StrictIn):
    brands: list[str] = Field(default_factory=list, max_length=10)
    aspects: list[str] = Field(default_factory=list, max_length=10)


def _summary(p: profile_mod.Profile) -> ProfileSummary:
    return ProfileSummary(
        id=p.id,
        name=p.name,
        category=p.category,
        focus_brand=p.focus_brand,
        rivals=p.rivals,
        keywords=p.keywords,
    )


class RiskFlag(BaseModel):
    brand: str
    score: int
    severity: Literal["high", "medium", "low"]
    categories: list[str]
    terms: list[str]
    evidence: str
    text: str
    comment_id: str
    video_id: str
    keyword: str
    like_count: int
    channel_title: str
    rank: int
    status: Literal["open", "handled", "dismissed"] = "open"
    updated_by: str | None = None


class RiskResponse(BaseModel):
    run: RunInfo
    summary: dict[str, dict]
    categories: dict[str, int]
    flags: list[RiskFlag]


class RiskStatusIn(StrictIn):
    comment_id: str = Field(min_length=1, max_length=120)
    brand: str = Field(min_length=1, max_length=50)
    status: Literal["open", "handled", "dismissed"]
    note: str = Field(default="", max_length=300)


# ---- auth endpoints ----------------------------------------------------------
def _set_session(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        COOKIE,
        token,
        max_age=s.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=s.cookie_secure,
        path="/",
    )


@app.post(
    "/api/v1/auth/signup",
    response_model=UserOut,
    status_code=201,
    tags=["auth"],
    responses={**WRITE, 409: {"model": ErrorBody, "description": "Email already registered"}},
)
def signup(
    body: SignupIn,
    response: Response,
    accounts: Accounts = Depends(get_accounts),
    _: None = Depends(csrf_for_anonymous),
) -> dict:
    user, token = accounts.open_session(accounts.signup(body.email, body.password, body.name))
    _set_session(response, token)
    return user.public()


@app.post(
    "/api/v1/auth/login",
    response_model=UserOut,
    tags=["auth"],
    responses={
        **WRITE,
        401: {"model": ErrorBody, "description": "Wrong email or password"},
        429: {"model": ErrorBody, "description": "Too many attempts"},
    },
)
def login(
    body: LoginIn,
    response: Response,
    accounts: Accounts = Depends(get_accounts),
    _: None = Depends(csrf_for_anonymous),
) -> dict:
    user, token = accounts.login(body.email, body.password)
    _set_session(response, token)
    return user.public()


@app.post("/api/v1/auth/logout", status_code=204, tags=["auth"], responses=WRITE)
def logout(
    request: Request, accounts: Accounts = Depends(get_accounts), _: None = Depends(csrf_for_anonymous)
) -> Response:
    token = request.cookies.get(COOKIE)
    if token:
        accounts.logout(token)
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/v1/auth/me", response_model=UserOut, tags=["auth"], responses=AUTH)
def me(user: User = Depends(current_user)) -> dict:
    return user.public()


# ---- ops ---------------------------------------------------------------------
@app.get("/health", response_model=Health, tags=["ops"])
def health(store: Store = Depends(get_store)) -> Health:
    s = get_settings()
    return Health(
        status="ok",
        version=__version__,
        has_data=store.latest_run() is not None,
        llm_configured=s.llm_configured,
        profile_configured=profile_mod.configured() or store.saved_profile() is not None,
    )


@app.get("/ready", response_model=Ready, tags=["ops"], responses={503: {"model": Ready}})
def ready(store: Store = Depends(get_store)) -> Response:
    """Readiness: database reachable and migrated; YouTube key and LLM configuration reported."""
    s = get_settings()
    checks: dict[str, Any] = {
        "database": store.db.ping(),
        "database_backend": store.db.label,
        "schema_version": store.db.schema_version() if store.db.ping() else None,
        "youtube_key": bool(s.youtube_api_key),
        "llm": "configured" if s.llm_configured else "off (rules only)",
        "llm_enabled": s.llm_enabled,
        "profile": profile_mod.configured() or store.saved_profile() is not None,
    }
    ok = bool(checks["database"])
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})


@app.get("/metrics", tags=["ops"], responses={**ADMIN, 200: {"content": {"text/plain": {}}}})
def metrics(store: Store = Depends(get_store), _: User = Depends(require_admin)) -> PlainTextResponse:
    """Prometheus text format: LLM calls, tokens, cost, schema-valid ratio, node latency."""
    return PlainTextResponse(obs.prometheus(store), media_type="text/plain; version=0.0.4")


@app.get("/api/v1/public/preview", tags=["ops"])
def public_preview(store: Store = Depends(get_store)) -> dict:
    """The landing page's live chart: overall share of voice from the latest real collection.

    Aggregates only (brand names and shares of public YouTube data); no comments, no users.
    Turn off with FANVAAZ_PUBLIC_PREVIEW=false.
    """
    s = get_settings()
    info = store.latest_run() if s.public_preview else None
    if info is None:
        return {"available": False}
    rep = store.report(info.run_id, "all")
    return {
        "available": True,
        "focus_brand": info.focus_brand,
        "collected_at": info.created_at,
        "videos": info.videos,
        "comments": info.comments,
        "brands": [{"brand": b.brand, "sov": round(b.sov, 4)} for b in (rep.brands if rep else [])][:10],
    }


@app.get("/api/v1/setup", tags=["ops"], responses=AUTH)
def setup_status(store: Store = Depends(get_store), _: User = Depends(current_user)) -> dict:
    """What a fresh install still needs: drives the onboarding banner."""
    s = get_settings()
    latest = store.latest_run()
    return {
        "profile_configured": profile_mod.configured() or store.saved_profile() is not None,
        "has_data": latest is not None,
        "youtube_key_set": bool(s.youtube_api_key),
        "llm_configured": s.llm_configured,
        "latest_run_at": latest.created_at if latest else None,
    }


# ---- data endpoints ----------------------------------------------------------
@app.get("/api/v1/brief", tags=["daily"], responses=AUTH)
def brief(store: Store = Depends(get_store), _: User = Depends(current_user)) -> dict:
    """One-sentence morning brief with changes since the previous run."""
    return build_brief(store)


@app.get("/api/v1/runs/latest", response_model=RunInfo, tags=["runs"], responses=NO_DATA)
def latest(info: RunInfo = Depends(latest_or_404)) -> RunInfo:
    return info


@app.get("/api/v1/keywords", response_model=list[str], tags=["sov"], responses=NO_DATA)
def keywords(info: RunInfo = Depends(latest_or_404), store: Store = Depends(get_store)) -> list[str]:
    ks = store.keywords(info.run_id)
    return ["all"] + [k for k in ks if k != "all"]


@app.get("/api/v1/sov", response_model=SovResponse, tags=["sov"], responses=NO_DATA)
def sov(
    keyword: str = Query("all", max_length=100),
    info: RunInfo = Depends(latest_or_404),
    store: Store = Depends(get_store),
) -> SovResponse:
    rep = store.report(info.run_id, keyword)
    if rep is None:
        raise HTTPException(404, f"Unknown keyword '{keyword}'. See /api/v1/keywords.")
    return SovResponse(run=info, focus_brand=info.focus_brand, report=rep)


@app.get("/api/v1/insights", tags=["sov"], responses=NO_DATA)
def insights(info: RunInfo = Depends(latest_or_404), store: Store = Depends(get_store)) -> dict:
    return {"run": info, "insights": store.insights(info.run_id)}


@app.get(
    "/api/v1/runs/{run_id}/evidence",
    tags=["runs"],
    responses={**AUTH, 404: {"model": ErrorBody, "description": "No such run"}},
)
def run_evidence(
    run_id: str = Path(pattern=r"^[0-9]{8}T[0-9]{6}-[a-f0-9]{6}$"),
    store: Store = Depends(get_store),
    _: User = Depends(current_user),
) -> dict:
    """The run's evidence pack: method, every insight with its numbers, citations and verification."""
    info = next((r for r in store.runs(200) if r.run_id == run_id), None)
    if info is None:
        raise HTTPException(404, "No such run.")
    return evidence_pack(info, store.insights(run_id), store.risks(run_id, limit=20), get_settings())


@app.get("/api/v1/risks", response_model=RiskResponse, tags=["risk"], responses=NO_DATA)
def risks(
    brand: str | None = Query(None, max_length=50),
    severity: Literal["high", "medium", "low"] | None = None,
    status: Literal["open", "handled", "dismissed"] | None = None,
    limit: int = Query(50, ge=1, le=1000),
    info: RunInfo = Depends(latest_or_404),
    store: Store = Depends(get_store),
) -> dict:
    """Comments that need a reply (safety, legal/consumer, product or service failure), with triage status."""
    statuses = store.risk_statuses()
    flags = []
    for f in store.risks(info.run_id, brand=brand, severity=severity, limit=100_000):
        st = statuses.get((f["comment_id"], f["brand"]), {})
        f = {**f, "status": st.get("status", "open"), "updated_by": st.get("updated_by")}
        if status is None or f["status"] == status:
            flags.append(f)
        if len(flags) >= limit:
            break
    return {
        "run": info,
        "summary": summarize(store.risks(info.run_id, limit=100_000)),
        "categories": {k: v["weight"] for k, v in RISK_CATEGORIES.items()},
        "flags": flags,
    }


@app.patch("/api/v1/risks/status", tags=["risk"], responses={**AUTH, **WRITE})
def set_risk_status(
    body: RiskStatusIn, store: Store = Depends(get_store), user: User = Depends(current_user)
) -> dict:
    """Mark a flagged comment handled or dismissed. The status carries over to later runs."""
    return store.set_risk_status(body.comment_id, body.brand, body.status, user.email, body.note)


def _csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection: comment text is attacker-controlled."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _csv(rows: list[dict], columns: list[str], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([_csv_safe("|".join(r[c]) if isinstance(r.get(c), list) else r.get(c)) for c in columns])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/api/v1/export/{table}.csv", tags=["export"], responses={**NO_DATA, 200: {"content": {"text/csv": {}}}}
)
def export(
    table: Literal["sov", "risks", "mentions"],
    info: RunInfo = Depends(latest_or_404),
    store: Store = Depends(get_store),
) -> Response:
    """Flat CSVs for Tableau / Power BI / Excel."""
    if table == "sov":
        rows = [
            {"run_id": info.run_id, "keyword": k, **b.model_dump()}
            for k in store.keywords(info.run_id)
            for b in store.report(info.run_id, k).brands
        ]
        cols = [
            "run_id",
            "keyword",
            "brand",
            "sov",
            "mention_share",
            "engagement_share",
            "positive_share",
            "net_sentiment",
            "raw_mentions",
            "positive",
            "negative",
            "videos_mentioning",
        ]
    elif table == "risks":
        st = store.risk_statuses()
        rows = [
            {
                "run_id": info.run_id,
                **r,
                "status": st.get((r["comment_id"], r["brand"]), {}).get("status", "open"),
            }
            for r in store.risks(info.run_id, limit=100_000)
        ]
        cols = [
            "run_id",
            "brand",
            "severity",
            "score",
            "status",
            "categories",
            "terms",
            "keyword",
            "rank",
            "channel_title",
            "like_count",
            "video_id",
            "comment_id",
            "evidence",
        ]
    else:
        rows = [{"run_id": info.run_id, **m.model_dump()} for m in store.mentions(info.run_id)]
        cols = ["run_id", "keyword", "video_id", "source", "source_id", "brand", "sentiment", "aspects"]
    return _csv(rows, cols, f"fanvaaz_{table}_{info.run_id}.csv")


@app.post(
    "/api/v1/ask",
    response_model=AskResponse,
    tags=["ask"],
    responses={**NO_DATA, **WRITE, 429: {"model": ErrorBody, "description": "Rate limited"}},
)
def ask(
    body: AskRequest,
    info: RunInfo = Depends(latest_or_404),
    store: Store = Depends(get_store),
    user: User = Depends(ask_rate_limit),
) -> dict:
    """Grounded answer with citations, reasoning steps, memory (pass `thread_id` back) and cache info."""
    obs.new_trace(info.run_id)
    thread = body.thread_id or uuid.uuid4().hex[:16]
    out = answerer_for(info, store).answer(body.question, user_key=user_key(user.id), thread_id=thread)
    out["thread_id"] = thread
    return out


@app.delete("/api/v1/ask/threads/{thread_id}", status_code=204, tags=["ask"], responses={**AUTH, **WRITE})
def forget_thread(
    thread_id: str = Path(pattern=THREAD),
    store: Store = Depends(get_store),
    user: User = Depends(current_user),
) -> Response:
    """Forget a conversation now (it would otherwise expire after FANVAAZ_MEMORY_TTL_HOURS)."""
    Memory(store).forget(thread_id, user_key(user.id))
    return Response(status_code=204)


@app.get("/api/v1/me/prefs", tags=["ask"], responses=AUTH)
def get_prefs(store: Store = Depends(get_store), user: User = Depends(current_user)) -> dict:
    """Long-term memory: the brands and aspects this user cares about (saved or learned)."""
    return Memory(store).prefs(user_key(user.id))


@app.put("/api/v1/me/prefs", tags=["ask"], responses={**AUTH, **WRITE})
def put_prefs(body: PrefsIn, store: Store = Depends(get_store), user: User = Depends(current_user)) -> dict:
    prof = profile_mod.active()
    brands = [b for b in body.brands if b in prof.brands]
    aspects = [a for a in body.aspects if a in prof.aspects]
    return Memory(store).set_prefs(user_key(user.id), brands, aspects)


# ---- admin -------------------------------------------------------------------
@app.get("/api/v1/admin/users", response_model=list[UserOut], tags=["admin"], responses=ADMIN)
def admin_users(accounts: Accounts = Depends(get_accounts), _: User = Depends(require_admin)) -> list[dict]:
    return [u.public() for u in accounts.list_users()]


@app.patch(
    "/api/v1/admin/users/{user_id}",
    response_model=UserOut,
    tags=["admin"],
    responses={
        **ADMIN,
        **WRITE,
        404: {"model": ErrorBody, "description": "No such user"},
        409: {"model": ErrorBody, "description": "Would remove the last admin"},
    },
)
def admin_update_user(
    body: UserPatch,
    user_id: int = Path(ge=0, le=2_147_483_647),
    accounts: Accounts = Depends(get_accounts),
    admin: User = Depends(require_admin),
    store: Store = Depends(get_store),
) -> dict:
    out = accounts.update_user(
        user_id, role=body.role, active=body.active, password=body.password, acting=admin
    ).public()
    audit(
        store,
        admin.email,
        "user.update",
        str(user_id),
        {"role": body.role, "active": body.active, "password_reset": body.password is not None},
    )
    return out


@app.get("/api/v1/admin/runs", response_model=list[RunInfo], tags=["admin"], responses=ADMIN)
def admin_runs(store: Store = Depends(get_store), _: User = Depends(require_admin)) -> list[RunInfo]:
    return store.runs(30)


@app.get("/api/v1/admin/system", tags=["admin"], responses=ADMIN)
def admin_system(
    store: Store = Depends(get_store),
    accounts: Accounts = Depends(get_accounts),
    _: User = Depends(require_admin),
) -> dict:
    s = get_settings()
    runs = store.runs(30)
    return {
        "version": __version__,
        "database": store.db.label,
        "youtube_key_set": bool(s.youtube_api_key),
        "llm_configured": s.llm_configured,
        "llm_enabled": s.llm_enabled,
        "llm_model": s.llm_model if s.llm_configured else None,
        "embed_model": s.embed_model if s.llm_configured else None,
        "daily_run_at": s.daily_run_at or None,
        "timezone": s.timezone,
        "signup_domains": [d.strip() for d in s.signup_domains.split(",") if d.strip()],
        "users": len(accounts.list_users()),
        "quota_units_today_estimate": sum(r.quota_units_used for r in runs[:1]),
        "daily_quota": 10_000,
        "mcp": {"http": MCP is not None, "read_token_set": bool(s.mcp_token)},
    }


@app.get("/api/v1/admin/ai-ops", tags=["admin"], responses=ADMIN)
def admin_ai_ops(
    days: int = Query(7, ge=1, le=90), store: Store = Depends(get_store), _: User = Depends(require_admin)
) -> dict:
    """LLM ledger: calls, tokens (estimated vs reported), cached tokens, cost, cache and schema-valid rates."""
    return {
        **obs.ai_ops(store, days),
        "governance": Governance(get_settings(), store).summary(),
        "prompts": prompts.catalog(),
    }


@app.get("/api/v1/admin/audit", tags=["admin"], responses=ADMIN)
def admin_audit(
    limit: int = Query(100, ge=1, le=1000),
    store: Store = Depends(get_store),
    _: User = Depends(require_admin),
) -> list[dict]:
    return audit_log(store, limit)


@app.get("/api/v1/admin/traces/{trace_id}", tags=["admin"], responses=ADMIN)
def admin_trace(
    trace_id: str = Path(pattern=r"^[a-f0-9]{16}$"), _: User = Depends(require_admin)
) -> list[dict]:
    """Every graph node span recorded for one trace (one run or one question)."""
    return obs.trace_spans(trace_id)


@app.get("/api/v1/admin/graphs", tags=["admin"], responses=ADMIN)
def admin_graphs(_: User = Depends(require_admin)) -> dict:
    """The two LangGraph agents as Mermaid diagrams."""
    from .graph import ask_graph, collect_graph

    return {"collect": collect_graph.mermaid(), "ask": ask_graph.mermaid()}


# ---- runs: synchronous (scheduler) and background jobs (UI) ---------------------
@app.post(
    "/api/v1/runs", response_model=RunInfo, status_code=201, tags=["runs"], responses={**ADMIN, **WRITE}
)
def create_run(
    body: RunRequest, store: Store = Depends(get_store), user: User = Depends(require_admin)
) -> RunInfo:
    """Collect live YouTube data and score it; returns when the run is stored (Cloud Scheduler uses this)."""
    try:
        info = run_pipeline(source=body.source, keywords=body.keywords, store=store, actor=user.email)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    clear_answerers()
    return info


_JOB_COLS = "job_id, status, started_at, finished_at, run_id, error, progress"


def _job(store: Store, job_id: str) -> dict | None:
    with store._conn() as c:
        r = c.execute(f"SELECT {_JOB_COLS} FROM run_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(zip(_JOB_COLS.split(", "), r, strict=True)) if r else None


def _run_job(store: Store, job_id: str, keywords: list[str] | None, actor: str) -> None:
    def progress(node: str) -> None:
        with store._conn() as c:
            c.execute("UPDATE run_jobs SET status = 'running', progress = ? WHERE job_id = ?", (node, job_id))

    try:
        info = run_pipeline(source="youtube", keywords=keywords, store=store, actor=actor, progress=progress)
        status, run_id, err = "done", info.run_id, None
        clear_answerers()
    except Exception as e:  # noqa: BLE001 - the job records the failure for the UI
        log.exception("run job %s failed", job_id)
        status, run_id, err = "failed", None, str(e)[:300]
    with store._conn() as c:
        c.execute(
            "UPDATE run_jobs SET status = ?, run_id = ?, error = ?, finished_at = ? WHERE job_id = ?",
            (status, run_id, err, datetime.now(UTC).isoformat(timespec="seconds"), job_id),
        )


@app.post(
    "/api/v1/runs/jobs", response_model=JobOut, status_code=202, tags=["runs"], responses={**ADMIN, **WRITE}
)
def start_run_job(
    body: RunRequest, store: Store = Depends(get_store), user: User = Depends(require_admin)
) -> dict:
    """Start a live collection in the background; poll GET /api/v1/runs/jobs/{job_id}."""
    s = get_settings()
    if not s.youtube_api_key:
        raise HTTPException(400, "Set FANVAAZ_YOUTUBE_API_KEY to collect live data")
    profile_mod.sync_from(store)
    with store._conn() as c:
        busy = c.execute("SELECT job_id FROM run_jobs WHERE status IN ('queued', 'running')").fetchone()
    if busy:
        raise HTTPException(409, f"A collection is already running (job {busy[0]}).")
    job_id = new_run_id()
    with store._conn() as c:
        c.execute(
            "INSERT INTO run_jobs (job_id, status, started_at, requested_by, progress) VALUES (?,?,?,?,?)",
            (job_id, "queued", datetime.now(UTC).isoformat(timespec="seconds"), user.email, "queued"),
        )
    threading.Thread(target=_run_job, args=(store, job_id, body.keywords, user.email), daemon=True).start()
    return _job(store, job_id)


@app.get(
    "/api/v1/runs/jobs/{job_id}",
    response_model=JobOut,
    tags=["runs"],
    responses={**ADMIN, 404: {"model": ErrorBody, "description": "No such job"}},
)
def get_run_job(
    job_id: str = Path(pattern=r"^[0-9]{8}T[0-9]{6}-[a-f0-9]{6}$"),
    store: Store = Depends(get_store),
    _: User = Depends(require_admin),
) -> dict:
    job = _job(store, job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    return job


# ---- tracking profile ----------------------------------------------------------------
@app.get("/api/v1/profile", response_model=ProfileSummary, tags=["profile"], responses=AUTH)
def get_profile(_: User = Depends(current_user)) -> ProfileSummary:
    """What is being tracked now. A change takes effect from the next run."""
    return _summary(profile_mod.active())


@app.get("/api/v1/admin/profile", response_model=ProfileAdmin, tags=["admin"], responses=ADMIN)
def admin_profile(store: Store = Depends(get_store), _: User = Depends(require_admin)) -> ProfileAdmin:
    saved = store.saved_profile() or {}
    try:
        active = profile_mod.sync_from(store)
    except profile_mod.NoProfileError:
        active = None
    return ProfileAdmin(
        active=active,
        updated_by=saved.get("updated_by"),
        updated_at=saved.get("updated_at"),
        presets=[_summary(p) for p in profile_mod.presets().values()],
    )


@app.put("/api/v1/admin/profile", response_model=ProfileAdmin, tags=["admin"], responses={**ADMIN, **WRITE})
def admin_set_profile(
    body: ProfilePut, store: Store = Depends(get_store), user: User = Depends(require_admin)
) -> ProfileAdmin:
    """Switch to a template or save a custom profile. Past runs keep the brands they tracked."""
    new = profile_mod.load(body.preset) if body.preset is not None else body.profile
    store.save_profile(new.model_dump(), user.email)
    profile_mod.set_active(new)
    clear_answerers()
    audit(
        store,
        user.email,
        "profile.set",
        new.id,
        {"focus_brand": new.focus_brand, "brands": list(new.brands), "keywords": new.keywords},
    )
    return admin_profile(store, user)


# ---- PWA and Android app support -----------------------------------------------------
@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    # Served from the root so its scope covers the whole app.
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/.well-known/assetlinks.json", include_in_schema=False)
def asset_links() -> JSONResponse:
    """Digital Asset Links for the Android app (FANVAAZ_ANDROID_ASSET_LINK="package:SHA256")."""
    link = get_settings().android_asset_link
    if ":" not in link:
        return JSONResponse([])
    package, fingerprint = link.split(":", 1)
    return JSONResponse(
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": package,
                    "sha256_cert_fingerprints": [fingerprint],
                },
            }
        ]
    )


# ---- pages -------------------------------------------------------------------
def _page(name: str) -> FileResponse:
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def landing() -> FileResponse:
    return _page("index.html")


@app.get("/login", include_in_schema=False)
def login_page(user: User | None = Depends(optional_user)):
    return RedirectResponse("/app", 303) if user else _page("login.html")


@app.get("/signup", include_in_schema=False)
def signup_page(user: User | None = Depends(optional_user)):
    return RedirectResponse("/app", 303) if user else _page("signup.html")


@app.get("/app", include_in_schema=False)
def app_page(user: User | None = Depends(optional_user)):
    return _page("app.html") if user else RedirectResponse("/login?next=/app", 303)


@app.get("/admin", include_in_schema=False)
def admin_page(user: User | None = Depends(optional_user)):
    if user is None:
        return RedirectResponse("/login?next=/admin", 303)
    return _page("admin.html") if user.role == "admin" else RedirectResponse("/app", 303)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
if MCP_APP is not None:
    app.mount("/mcp", MCP_APP, name="mcp")
