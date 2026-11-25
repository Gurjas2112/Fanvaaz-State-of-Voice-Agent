"""Collector against a fake YouTube API (httpx.MockTransport): no network, no key."""

import json

import httpx
import pytest

from fanvaaz.collector import QuotaExceeded, YouTubeCollector
from fanvaaz.config import Settings
from fanvaaz.text import contains_pii


def fake_youtube(request: httpx.Request) -> httpx.Response:
    path = request.url.path.rsplit("/", 1)[-1]
    q = request.url.params
    if path == "search":
        assert q["maxResults"] == "3" and q["regionCode"] == "IN" and q["key"] == "k"
        return httpx.Response(200, json={"items": [{"id": {"videoId": f"v{i}"}} for i in (1, 2, 3)]})
    if path == "videos":
        ids = q["id"].split(",")
        # API returns items out of order and drops v3 (deleted video)
        items = [
            {
                "id": i,
                "snippet": {
                    "title": f"Title {i} &amp; Atomberg",
                    "channelTitle": "Ch",
                    "description": "call 9876543210",
                },
                "statistics": {"viewCount": "100", "likeCount": "5", "commentCount": "2"},
            }
            for i in reversed(ids)
            if i != "v3"
        ]
        return httpx.Response(200, json={"items": items})
    if path == "commentThreads":
        if q["videoId"] == "v2":
            return httpx.Response(403, json={"error": {"errors": [{"reason": "commentsDisabled"}]}})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "snippet": {
                            "topLevelComment": {
                                "id": "c1",
                                "snippet": {
                                    "textDisplay": "Atomberg is great, mail x@y.com",
                                    "likeCount": 3,
                                    "authorChannelId": {"value": "UC123"},
                                },
                            }
                        }
                    }
                ]
            },
        )
    return httpx.Response(404)


def collector(tmp_path, handler=fake_youtube):
    s = Settings(youtube_api_key="k", top_n=3, comments_per_video=5)
    return YouTubeCollector(s, client=httpx.Client(transport=httpx.MockTransport(handler)), run_dir=tmp_path)


def test_collect_ranks_stats_and_quota(tmp_path):
    col = collector(tmp_path)
    videos = col.collect(["smart fan"])
    assert [v.video_id for v in videos] == ["v1", "v2"]  # search order kept, deleted video skipped
    assert [v.rank for v in videos] == [1, 2]
    assert videos[0].title == "Title v1 & Atomberg"
    assert videos[1].comments == []  # comments disabled handled, not an error
    assert col.units == 100 + 1 + 1  # search + videos + one successful commentThreads


def test_pii_redacted_at_ingestion(tmp_path):
    v = collector(tmp_path).collect(["smart fan"])[0]
    assert not contains_pii(v.description)
    assert not contains_pii(v.comments[0].text)
    assert v.comments[0].author_hash and "UC123" not in v.comments[0].author_hash


def test_raw_responses_saved_for_replay(tmp_path):
    collector(tmp_path).collect(["smart fan"])
    saved = json.loads((tmp_path / "search_smart fan.json").read_text())
    assert len(saved["items"]) == 3


def test_quota_exceeded_is_raised(tmp_path):
    def handler(req):
        return httpx.Response(403, text='{"error":{"errors":[{"reason":"quotaExceeded"}]}}')

    with pytest.raises(QuotaExceeded):
        collector(tmp_path, handler).collect(["smart fan"])


def test_missing_key_fails_fast():
    with pytest.raises(RuntimeError):
        YouTubeCollector(Settings(youtube_api_key=None))
