"""Things Model Hub does by itself, on a timer: a saved backup now and then, a look for new
uploads from designers you follow, a warning when a spool or a supply runs low, a gentle check
of linked listings for changes, and noticing when a printer finishes a print.

One loop wakes every half minute and runs whatever is due. Each job is wrapped so a failure in
one never stops the others, and nothing runs more often than its interval.
"""
import asyncio
import logging
import time
from typing import Callable, Optional

from sqlmodel import Session

from app.db import engine

logger = logging.getLogger("modelhub.scheduler")

TICK_SECONDS = 30
INTERVALS = {                     # seconds between runs of each job
    "printers": 30,
    "backup": 3600,
    "designers": 6 * 3600,
    "low_stock": 6 * 3600,
    "listing_updates": 24 * 3600,
}

_last_run: dict = {}


def _jobs() -> dict:
    from app import auto_backup, follow_watch, printwatch, source_updates_auto, stock_watch
    return {
        "printers": printwatch.poll,
        "backup": auto_backup.maybe_backup,
        "designers": follow_watch.check_and_notify,
        "low_stock": stock_watch.check_and_notify,
        "listing_updates": source_updates_auto.maybe_start,
    }


def run_due(now: Optional[float] = None, only: Optional[list] = None) -> list:
    """Run every job whose interval has passed; returns the names that ran."""
    now = time.time() if now is None else now
    ran = []
    for name, job in _jobs().items():
        if only is not None and name not in only:
            continue
        if now - _last_run.get(name, 0) < INTERVALS[name]:
            continue
        _last_run[name] = now
        try:
            with Session(engine) as session:
                job(session)
            ran.append(name)
        except Exception:
            logger.exception("Scheduled job %s failed", name)
    return ran


def reset() -> None:
    _last_run.clear()


async def loop() -> None:
    await asyncio.sleep(20)          # let the app finish starting first
    while True:
        try:
            await asyncio.to_thread(run_due)
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
