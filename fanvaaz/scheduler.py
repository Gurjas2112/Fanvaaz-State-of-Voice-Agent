"""Optional daily collection so the brief is fresh every morning.

Set FANVAAZ_DAILY_RUN_AT="06:30" (local time in FANVAAZ_TIMEZONE). With several
web workers, each one runs this loop, but a row in `schedule_claims` keyed by
date makes sure only one of them collects per day.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .store import Store

log = logging.getLogger("fanvaaz.scheduler")


def next_fire(now: datetime, at: str) -> datetime:
    hh, mm = (int(x) for x in at.split(":"))
    fire = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return fire if fire > now else fire + timedelta(days=1)


def claim(store: Store, day: str) -> bool:
    with store._conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS schedule_claims (day TEXT PRIMARY KEY)")
        return c.execute("INSERT OR IGNORE INTO schedule_claims VALUES (?)", (day,)).rowcount == 1


async def daily_loop(settings: Settings, store: Store) -> None:
    from .pipeline import run

    tz = ZoneInfo(settings.timezone)
    source = settings.daily_run_source
    if source == "youtube" and not settings.youtube_api_key:
        log.warning("daily run disabled: FANVAAZ_YOUTUBE_API_KEY is not set")
        return
    while True:
        now = datetime.now(tz)
        fire = next_fire(now, settings.daily_run_at)
        await asyncio.sleep((fire - now).total_seconds())
        if claim(store, fire.date().isoformat()):
            try:
                info = await asyncio.to_thread(run, source=source, settings=settings, store=store)
                log.info("daily run %s done", info.run_id)
            except Exception:  # noqa: BLE001 - keep the loop alive; the failure is logged
                log.exception("daily run failed")
