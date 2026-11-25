"""One run = collect -> extract -> score -> explain -> persist."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from . import insights as insights_mod
from . import risk as risk_mod
from .collector import YouTubeCollector, load_demo
from .config import DEFAULT_KEYWORDS, Settings, get_settings
from .extract import extract_all
from .llm import LLM, get_llm
from .models import RunInfo
from .sov import by_keyword
from .store import Store

log = logging.getLogger("atompulse.pipeline")


def run(
    source: str = "demo",
    keywords: list[str] | None = None,
    settings: Settings | None = None,
    store: Store | None = None,
    llm: LLM | None = None,
    use_llm: bool = True,
) -> RunInfo:
    s = settings or get_settings()
    store = store or Store(s.db_path)
    keywords = keywords or DEFAULT_KEYWORDS
    llm = llm if llm is not None else (get_llm(s) if use_llm else None)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]

    units = 0
    if source == "youtube":
        col = YouTubeCollector(s, run_dir=s.raw_dir / run_id)
        videos = col.collect(keywords)
        units = col.units
        src = "youtube"
    elif source == "demo":
        videos = [v for v in load_demo(s.demo_data) if v.keyword in keywords]
        src = "synthetic-demo"
    else:
        raise ValueError("source must be 'youtube' or 'demo'")

    mentions = extract_all(videos, llm=llm)
    reports = by_keyword(videos, mentions, s)
    risks = risk_mod.flag_comments(videos, mentions)
    ins = insights_mod.build(reports, videos, mentions, risks=risks)
    if llm is not None:
        ins = insights_mod.rewrite_with_llm(ins, llm)

    info = RunInfo(
        run_id=run_id,
        source=src,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        keywords=sorted({v.keyword for v in videos}),
        videos=len(videos),
        comments=sum(len(v.comments) for v in videos),
        quota_units_used=units,
    )
    store.save_run(info, videos, mentions, reports, ins, risks)
    log.info(
        "run %s saved: %d videos, %d mentions, %d quota units", run_id, len(videos), len(mentions), units
    )
    return info
