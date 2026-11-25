"""FastAPI service: JSON API + the dashboard at /.

OpenAPI docs at /docs. The Postman collection, Schemathesis, Playwright, Appium
and promptfoo suites all run against this app.
"""

from __future__ import annotations

import logging
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .ask import Answerer
from .config import FOCUS_BRAND, get_settings
from .llm import get_llm
from .models import KeywordReport, RunInfo
from .pipeline import run as run_pipeline
from .store import Store

log = logging.getLogger("atompulse.api")
STATIC = Path(__file__).parent / "static"

app = FastAPI(
    title="AtomPulse API",
    version=__version__,
    description="YouTube Share of Voice for Atomberg vs competitors, with guarded insights and Q&A.",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    return response


@lru_cache
def get_store() -> Store:
    return Store(get_settings().db_path)


_answerers: dict[str, Answerer] = {}


def latest_or_404(store: Store = Depends(get_store)) -> RunInfo:
    info = store.latest_run()
    if not info:
        raise HTTPException(404, "No runs yet. POST /api/v1/runs or run `atompulse run --source demo`.")
    return info


def answerer_for(info: RunInfo, store: Store) -> Answerer:
    if info.run_id not in _answerers:
        _answerers.clear()
        reports = {k: store.report(info.run_id, k) for k in store.keywords(info.run_id)}
        _answerers[info.run_id] = Answerer(
            store.videos(info.run_id), store.mentions(info.run_id), reports, get_llm()
        )
    return _answerers[info.run_id]


class ErrorBody(BaseModel):
    detail: str


NO_DATA = {404: {"model": ErrorBody, "description": "No runs yet, or unknown keyword"}}
BAD_BODY = {400: {"model": ErrorBody, "description": "Body is not valid JSON"}}


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    has_data: bool
    llm_configured: bool


class SovResponse(BaseModel):
    run: RunInfo
    focus_brand: str
    report: KeywordReport


class AskRequest(BaseModel):
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


class RunRequest(BaseModel):
    source: Literal["youtube", "demo"] = "demo"
    keywords: list[str] | None = Field(default=None, max_length=10)


@app.get("/health", response_model=Health, tags=["ops"])
def health(store: Store = Depends(get_store)) -> Health:
    s = get_settings()
    return Health(
        status="ok",
        version=__version__,
        has_data=store.latest_run() is not None,
        llm_configured=bool(s.llm_base_url and s.llm_api_key),
    )


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


@app.post("/api/v1/ask", response_model=AskResponse, tags=["ask"], responses={**NO_DATA, **BAD_BODY})
def ask(body: AskRequest, info: RunInfo = Depends(latest_or_404), store: Store = Depends(get_store)) -> dict:
    out = answerer_for(info, store).answer(body.question)
    return {k: out[k] for k in ("answer", "refused", "reasons", "mode", "sources")}


@app.post(
    "/api/v1/runs",
    response_model=RunInfo,
    status_code=201,
    tags=["runs"],
    responses={**BAD_BODY, 401: {"model": ErrorBody, "description": "Missing or wrong admin token"}},
)
def create_run(
    body: RunRequest, x_admin_token: str | None = Header(default=None), store: Store = Depends(get_store)
) -> RunInfo:
    expected = get_settings().admin_token
    if not expected or not x_admin_token or not secrets.compare_digest(expected, x_admin_token):
        raise HTTPException(401, "Missing or wrong X-Admin-Token")
    try:
        return run_pipeline(source=body.source, keywords=body.keywords, store=store)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error"})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
