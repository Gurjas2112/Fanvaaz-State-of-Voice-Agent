"""Shared fixtures: an isolated database seeded from a recorded REAL YouTube
collection (tests/fixtures/youtube_snapshot), and an API client.

The snapshot is the processed output of one live run (PII-redacted, commenter
ids hashed), replayed through the full collection graph, so every offline test
runs on real comments without a key or the network. Re-record it with
scripts/record_snapshot.py.

Environment is set before fanvaaz is imported so no test can touch a real DB,
a real LLM or the YouTube API by accident.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="fanvaaz-test-"))
os.environ["FANVAAZ_DB_PATH"] = str(_TMP / "test.db")
os.environ["FANVAAZ_RAW_DIR"] = str(_TMP / "raw")
os.environ["FANVAAZ_CHECKPOINT_PATH"] = str(_TMP / "checkpoints.db")
os.environ["FANVAAZ_PROFILE"] = "smartphones-india"
os.environ["FANVAAZ_ASK_RATE_PER_MIN"] = "600"  # the red-team suite asks ~100 questions
# FANVAAZ_TEST_DATABASE_URL runs the whole suite on Postgres (the CI postgres job); else SQLite.
# Empty strings (not unset) so a developer's .env can never leak a real database or key into tests:
# pydantic-settings prefers process env over .env.
os.environ["FANVAAZ_DATABASE_URL"] = os.environ.get("FANVAAZ_TEST_DATABASE_URL", "")
os.environ["FANVAAZ_API_TOKEN"] = "test-service-token"
os.environ["FANVAAZ_ADMIN_EMAIL"] = "admin@fanvaaz.test"
os.environ["FANVAAZ_ADMIN_PASSWORD"] = "Admin-pass-2026!"
os.environ["FANVAAZ_SIGNUP_DOMAINS"] = ""


# The opt-in suites need real keys (shell env or .env): the LLM evals (pytest -m llm) under
# LLM_EVAL_*, the live YouTube test (pytest -m live) under LIVE_YOUTUBE_API_KEY. The app under
# test never sees them.
def _dotenv() -> dict:
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v}


_ENV = {**_dotenv(), **{k: v for k, v in os.environ.items() if v}}
for src, dst in (
    ("FANVAAZ_LLM_BASE_URL", "LLM_EVAL_BASE_URL"),
    ("FANVAAZ_LLM_API_KEY", "LLM_EVAL_API_KEY"),
    ("FANVAAZ_LLM_MODEL", "LLM_EVAL_MODEL"),
    ("FANVAAZ_YOUTUBE_API_KEY", "LIVE_YOUTUBE_API_KEY"),
):
    if _ENV.get(src) and not os.environ.get(dst):
        os.environ[dst] = _ENV[src]
for var in (
    "FANVAAZ_LLM_BASE_URL",
    "FANVAAZ_LLM_API_KEY",
    "FANVAAZ_YOUTUBE_API_KEY",
    "FANVAAZ_MCP_TOKEN",
    "FANVAAZ_DAILY_RUN_AT",
):
    os.environ[var] = ""
os.environ["FANVAAZ_LLM_ENABLED"] = "true"

import pytest  # noqa: E402

from fanvaaz.config import get_settings  # noqa: E402
from fanvaaz.pipeline import run  # noqa: E402
from fanvaaz.store import Store  # noqa: E402

GOLDEN = Path(__file__).parent / "golden"
SNAPSHOT = Path(__file__).parent / "fixtures" / "youtube_snapshot" / "smartphones-india.json"
REPORTS = Path(__file__).resolve().parent.parent / "reports"


@pytest.fixture(scope="session")
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def seeded_store(settings) -> Store:
    store = Store.from_settings(settings)
    if store.latest_run() is None:
        run(source="replay", snapshot=SNAPSHOT, settings=settings, store=store, use_llm=False)
    return store


@pytest.fixture(scope="session")
def latest(seeded_store):
    info = seeded_store.latest_run()
    reports = {k: seeded_store.report(info.run_id, k) for k in seeded_store.keywords(info.run_id)}
    return {
        "info": info,
        "reports": reports,
        "videos": seeded_store.videos(info.run_id),
        "mentions": seeded_store.mentions(info.run_id),
        "insights": seeded_store.insights(info.run_id),
    }


@pytest.fixture(scope="session")
def client(seeded_store):
    from fastapi.testclient import TestClient

    from fanvaaz.api import app, get_store

    app.dependency_overrides[get_store] = lambda: seeded_store
    with TestClient(app, headers={"Authorization": "Bearer test-service-token"}) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def anon(client):
    """A cookie-based browser-like client with no credentials (shares the app and store)."""
    from fastapi.testclient import TestClient

    from fanvaaz.api import app

    with TestClient(app, headers={"X-Requested-With": "fanvaaz"}) as c:
        yield c


ADMIN = {"email": "admin@fanvaaz.test", "password": "Admin-pass-2026!"}


@pytest.fixture(scope="session")
def reports_dir() -> Path:
    REPORTS.mkdir(exist_ok=True)
    return REPORTS


@pytest.fixture(autouse=True)
def _default_profile():
    """Every test starts on the smartphones example profile, whatever the previous test switched to."""
    from fanvaaz import profile

    profile.reset()
    yield
    profile.reset()
