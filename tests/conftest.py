"""Shared fixtures: an isolated database seeded with one demo run, and an API client.

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
os.environ["FANVAAZ_API_TOKEN"] = "test-service-token"
os.environ["FANVAAZ_ADMIN_EMAIL"] = "admin@fanvaaz.test"
os.environ["FANVAAZ_ADMIN_PASSWORD"] = "Admin-pass-2026!"
os.environ["FANVAAZ_SIGNUP_DOMAINS"] = ""
for var in ("FANVAAZ_LLM_BASE_URL", "FANVAAZ_LLM_API_KEY", "FANVAAZ_YOUTUBE_API_KEY"):
    os.environ.pop(var, None)

import pytest  # noqa: E402

from fanvaaz.config import get_settings  # noqa: E402
from fanvaaz.pipeline import run  # noqa: E402
from fanvaaz.store import Store  # noqa: E402

GOLDEN = Path(__file__).parent / "golden"
REPORTS = Path(__file__).resolve().parent.parent / "reports"


@pytest.fixture(scope="session")
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def seeded_store(settings) -> Store:
    store = Store(settings.db_path)
    if store.latest_run() is None:
        run(source="demo", settings=settings, store=store, use_llm=False)
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
