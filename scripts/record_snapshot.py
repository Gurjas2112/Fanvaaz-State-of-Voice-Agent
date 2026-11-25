"""Record a replayable snapshot of one real YouTube collection.

    FANVAAZ_YOUTUBE_API_KEY=... python scripts/record_snapshot.py \
        --profile smartphones-india --top-n 25 --comments 50 \
        --out tests/fixtures/youtube_snapshot/smartphones-india.json

The snapshot stores processed videos only: text is HTML-cleaned and PII-redacted
(emails, phone numbers) and commenter ids are SHA-256 hashes; author names are
never kept. The offline test suite replays it, so CI tests run on real comments
without a key or the network. Quota: about (100 + 1 + top_n) units per keyword.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from fanvaaz import profile as profile_mod
from fanvaaz.collector import YouTubeCollector, save_snapshot
from fanvaaz.config import get_settings


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default="smartphones-india")
    p.add_argument("--top-n", type=int, default=25)
    p.add_argument("--comments", type=int, default=50)
    p.add_argument("--keywords", nargs="*", default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    s = get_settings().model_copy(
        update={"top_n": args.top_n, "comments_per_video": args.comments, "save_raw": False}
    )
    prof = profile_mod.set_active(profile_mod.load(args.profile))
    keywords = args.keywords or prof.keywords
    col = YouTubeCollector(s)
    videos = col.collect(keywords)
    out = Path(args.out or f"tests/fixtures/youtube_snapshot/{prof.id}.json")
    meta = {
        "source": "YouTube Data API v3 (search.list, videos.list, commentThreads.list)",
        "profile_id": prof.id,
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "region_code": s.region_code,
        "keywords": keywords,
        "top_n": args.top_n,
        "comments_per_video": args.comments,
        "quota_units": col.units,
        "privacy": "text PII-redacted at ingestion; commenter ids hashed; no author names",
    }
    save_snapshot(videos, out, meta)
    n_comments = sum(len(v.comments) for v in videos)
    print(f"saved {out}: {len(videos)} videos, {n_comments} comments, {col.units} quota units")


if __name__ == "__main__":
    main()
