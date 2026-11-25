"""FastAPI service: pages, auth, JSON API.

Pages   /  /login  /signup  /app (signed in)  /admin (admins)
API     /api/v1/...  (signed-in session cookie, or `Authorization: Bearer <FANVAAZ_API_TOKEN>`)
Docs    /docs (OpenAPI). Postman, Schemathesis, Playwright, Appium and promptfoo run against this app.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path as FilePath
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .ask import Answerer
from .auth import COOKIE, CSRF_HEADER, CSRF_VALUE, EMAIL_PATTERN, Accounts, AuthError, User
from .brief import build_brief
from .config import FOCUS_BRAND, get_settings
from .llm import get_llm
from .models import KeywordReport, RunInfo
from .pipeline import run as run_pipeline
from .risk import RISK_CATEGORIES, summarize
from .store import Store

log = logging.getLogger("fanvaaz.api")
STATIC = FilePath(__file__).parent / "static"


@lru_cache
def get_store() -> Store:
    return Store(get_settings().db_path)


_accounts: dict[int, Accounts] = {}


def get_accounts(store: Store = Depends(get_store)) -> Accounts:
    key = id(store)
    if key not in _accounts:
        _accounts[key] = Accounts(store, get_settings())
    return _accounts[key]


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    store = app.dependency_overrides.get(get_store, get_store)()
    get_accounts(store).ensure_admin()
    task = None
    if s.daily_run_at:
        from .scheduler import daily_loop

        task = asyncio.create_task(daily_loop(s, store))
    yield
    if task:
        task.cancel()


app = FastAPI(
    title="Fanvaaz API",
    version=__version__,
    description="Atomberg's daily YouTube Share of Voice, risk inbox and guarded Q&A.",
    lifespan=lifespan,
)

CSP = (
    "default-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' data:; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def headers(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
        response.headers["Content-Security-Policy"] = CSP
    return response


@app.exception_handler(AuthError)
async def auth_error(request: Request, exc: AuthError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


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
        raise HTTPException(404, "No data yet. An admin can start a run from the admin page.")
    return info


_answerers: dict[str, Answerer] = {}


def answerer_for(info: RunInfo, store: Store) -> Answerer:
    if info.run_id not in _answerers:
        _answerers.clear()
        reports = {k: store.report(info.run_id, k) for k in store.keywords(info.run_id)}
        _answerers[info.run_id] = Answerer(
            store.videos(info.run_id), store.mentions(info.run_id), reports, get_llm()
        )
    return _answerers[info.run_id]


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


class AskRequest(StrictIn):
    question: str = Field(min_length=1, max_length=1000, examples=["Why do people complain about Atomberg?"])


class Source(BaseModel):
    comment_id: str
    video_id: str
    keyword: str
    text: str


class AskResponse(BaseModel):
    answer: str
    refused: bool
    reasons: list[str]
    mode: Literal["guard", "extractive", "llm"]
    sources: list[Source]


class RunRequest(StrictIn):
    source: Literal["youtube", "demo"] = "demo"
    keywords: list[str] | None = Field(default=None, max_length=10)


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


# ---- data endpoints ----------------------------------------------------------
@app.get("/health", response_model=Health, tags=["ops"])
def health(store: Store = Depends(get_store)) -> Health:
    s = get_settings()
    return Health(
        status="ok",
        version=__version__,
        has_data=store.latest_run() is not None,
        llm_configured=bool(s.llm_base_url and s.llm_api_key),
    )


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
    return SovResponse(run=info, focus_brand=FOCUS_BRAND, report=rep)


@app.get("/api/v1/insights", tags=["sov"], responses=NO_DATA)
def insights(info: RunInfo = Depends(latest_or_404), store: Store = Depends(get_store)) -> dict:
    return {"run": info, "insights": store.insights(info.run_id)}


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


@app.post("/api/v1/ask", response_model=AskResponse, tags=["ask"], responses={**NO_DATA, **WRITE})
def ask(
    body: AskRequest,
    info: RunInfo = Depends(latest_or_404),
    store: Store = Depends(get_store),
    _: User = Depends(current_user),
) -> dict:
    out = answerer_for(info, store).answer(body.question)
    return {k: out[k] for k in ("answer", "refused", "reasons", "mode", "sources")}


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
) -> dict:
    return accounts.update_user(
        user_id, role=body.role, active=body.active, password=body.password, acting=admin
    ).public()


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
        "youtube_key_set": bool(s.youtube_api_key),
        "llm_configured": bool(s.llm_base_url and s.llm_api_key),
        "llm_model": s.llm_model if s.llm_base_url else None,
        "daily_run_at": s.daily_run_at or None,
        "timezone": s.timezone,
        "signup_domains": [d.strip() for d in s.signup_domains.split(",") if d.strip()],
        "users": len(accounts.list_users()),
        "quota_units_today_estimate": sum(r.quota_units_used for r in runs[:1]),
        "daily_quota": 10_000,
    }


@app.post(
    "/api/v1/runs", response_model=RunInfo, status_code=201, tags=["runs"], responses={**ADMIN, **WRITE}
)
def create_run(
    body: RunRequest, store: Store = Depends(get_store), _: User = Depends(require_admin)
) -> RunInfo:
    try:
        info = run_pipeline(source=body.source, keywords=body.keywords, store=store)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    _answerers.clear()
    return info


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
