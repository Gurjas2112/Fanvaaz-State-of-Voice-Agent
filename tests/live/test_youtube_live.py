"""Live end-to-end: the real YouTube Data API through the full collection graph.

    FANVAAZ_YOUTUBE_API_KEY=... pytest -m live      (or put the key in .env)

Small on purpose (one keyword, 5 videos, 10 comments each: about 106 quota units),
so it can run before every release without denting the 10,000-unit daily quota.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live
KEY = os.getenv("LIVE_YOUTUBE_API_KEY")
if not KEY:
    pytest.skip("live tests need FANVAAZ_YOUTUBE_API_KEY (env or .env)", allow_module_level=True)


def test_live_collection_through_the_graph(tmp_path, monkeypatch):
    from fanvaaz import profile
    from fanvaaz.config import get_settings
    from fanvaaz.pipeline import run
    from fanvaaz.store import Store
    from fanvaaz.text import contains_pii

    s = get_settings().model_copy(
        update={
            "youtube_api_key": KEY,
            "top_n": 5,
            "comments_per_video": 10,
            "raw_dir": tmp_path / "raw",
            "checkpoint_path": tmp_path / "ckpt.db",
            "save_raw": False,
        }
    )
    store = Store(tmp_path / "live.db")
    profile.set_active(profile.load("smartphones-india"))
    info = run(source="youtube", keywords=["smartphone camera test"], settings=s, store=store, use_llm=False)

    assert info.source == "youtube" and info.videos >= 3 and 0 < info.quota_units_used <= 120
    videos = store.videos(info.run_id)
    assert all(v.video_id and v.title for v in videos)
    texts = [c.text for v in videos for c in v.comments]
    assert texts, "real videos should have comments"
    assert not any(contains_pii(t) for t in texts)  # redacted at ingestion
    assert store.report(info.run_id, "all") is not None
