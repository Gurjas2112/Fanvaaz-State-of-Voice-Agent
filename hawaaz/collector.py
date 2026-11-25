"""YouTube Data API v3 collector.

Quota per keyword (default N=50, 100 comments/video):
  search.list        100 units  (one page of 50 results)
  videos.list          1 unit   (statistics for all 50 ids at once)
  commentThreads.list  1 unit per video  (50 units)
  -> about 151 units per keyword, 906 for the 6 default keywords, of a 10,000/day free quota.

Every raw API response is saved under data/raw/<run_id>/ so runs can be replayed
and tests never need the network.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, get_settings
from .models import Comment, Video
from .text import clean, hash_id, prepare

log = logging.getLogger("hawaaz.collector")

API = "https://www.googleapis.com/youtube/v3"
COST = {"search": 100, "videos": 1, "commentThreads": 1}


class QuotaExceeded(RuntimeError):
    pass


class YouTubeCollector:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        run_dir: Path | None = None,
    ):
        self.s = settings or get_settings()
        if not self.s.youtube_api_key:
            raise RuntimeError("Set HAWAAZ_YOUTUBE_API_KEY to collect live data")
        self.client = client or httpx.Client(timeout=20)
        self.units = 0
        self.run_dir = run_dir

    def _get(self, endpoint: str, params: dict[str, Any], save_as: str | None = None) -> dict[str, Any]:
        params = {**params, "key": self.s.youtube_api_key}
        for attempt in range(4):
            r = self.client.get(f"{API}/{endpoint}", params=params)
            if r.status_code == 403 and "quotaExceeded" in r.text:
                raise QuotaExceeded("YouTube daily quota exhausted")
            if r.status_code == 403 and "commentsDisabled" in r.text:
                return {"items": [], "commentsDisabled": True}
            if r.status_code in (429, 500, 503):
                time.sleep(2**attempt)
                continue
            r.raise_for_status()
            self.units += COST[endpoint]
            data = r.json()
            if self.run_dir and save_as:
                path = self.run_dir / f"{save_as}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        r.raise_for_status()
        return {}

    def search(self, keyword: str) -> list[str]:
        data = self._get(
            "search",
            {
                "part": "snippet",
                "q": keyword,
                "type": "video",
                "maxResults": self.s.top_n,
                "regionCode": self.s.region_code,
                "order": "relevance",
            },
            save_as=f"search_{keyword}",
        )
        return [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]

    def videos(self, ids: list[str], keyword: str) -> list[Video]:
        if not ids:
            return []
        data = self._get(
            "videos", {"part": "snippet,statistics", "id": ",".join(ids)}, save_as=f"videos_{keyword}"
        )
        by_id = {it["id"]: it for it in data.get("items", [])}
        out = []
        for rank, vid in enumerate(ids, start=1):
            it = by_id.get(vid)
            if not it:
                continue
            sn, st = it.get("snippet", {}), it.get("statistics", {})
            out.append(
                Video(
                    video_id=vid,
                    keyword=keyword,
                    rank=rank,
                    title=clean(sn.get("title", "")),
                    description=prepare(sn.get("description", "")),
                    channel_title=clean(sn.get("channelTitle", "")),
                    published_at=sn.get("publishedAt", ""),
                    view_count=int(st.get("viewCount", 0)),
                    like_count=int(st.get("likeCount", 0)),
                    comment_count=int(st.get("commentCount", 0)),
                )
            )
        return out

    def comments(self, video_id: str) -> list[Comment]:
        if self.s.comments_per_video == 0:
            return []
        data = self._get(
            "commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": self.s.comments_per_video,
                "order": "relevance",
                "textFormat": "plainText",
            },
            save_as=f"comments_{video_id}",
        )
        out = []
        for it in data.get("items", []):
            top = it["snippet"]["topLevelComment"]
            sn = top["snippet"]
            out.append(
                Comment(
                    comment_id=top["id"],
                    text=prepare(sn.get("textDisplay", "")),
                    like_count=int(sn.get("likeCount", 0)),
                    author_hash=hash_id(sn.get("authorChannelId", {}).get("value", "")),
                )
            )
        return out

    def collect(self, keywords: list[str]) -> list[Video]:
        videos: list[Video] = []
        fetched_comments: dict[str, list[Comment]] = {}
        for kw in keywords:
            vids = self.videos(self.search(kw), kw)
            for v in vids:
                if v.video_id not in fetched_comments:  # a video ranking twice is fetched once
                    fetched_comments[v.video_id] = self.comments(v.video_id)
                v.comments = fetched_comments[v.video_id]
            videos.extend(vids)
            log.info("keyword=%r videos=%d units=%d", kw, len(vids), self.units)
        return videos


def load_demo(path: Path) -> list[Video]:
    """Load the bundled synthetic demo dataset (no network, no key)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    videos = []
    for v in data["videos"]:
        v = dict(v)
        v["description"] = prepare(v.get("description", ""))
        v["comments"] = [{**c, "text": prepare(c["text"])} for c in v.get("comments", [])]
        videos.append(Video.model_validate(v))
    return videos
