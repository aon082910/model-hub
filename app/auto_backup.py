"""Saved backups on a schedule (Settings: off, daily or weekly; the newest N are kept)."""
import logging
import time

from sqlmodel import Session

from app import backup
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

logger = logging.getLogger("modelhub.autobackup")

PERIODS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600}
PREFIX = "auto-backup"
RETRY_AFTER_FAILURE = 6 * 3600


def keep_count(session: Session) -> int:
    try:
        return max(1, min(60, int(get_setting(session, "auto_backup_keep", "") or "7")))
    except ValueError:
        return 7


def maybe_backup(session: Session, now: float = None) -> bool:
    """Make a scheduled backup if one is due. Returns True if one was made."""
    now = time.time() if now is None else now
    period = PERIODS.get(get_setting(session, "auto_backup", "") or "weekly")
    if period is None:                       # "off" (or anything else)
        return False
    try:
        last = float(get_setting(session, "auto_backup_last", "0") or 0)
    except ValueError:
        last = 0
    if now - last < period:
        return False
    try:
        backup.save_backup(PREFIX)
        backup.prune(PREFIX, keep_count(session))
    except Exception as e:
        logger.exception("Scheduled backup failed")
        set_setting(session, "auto_backup_last", str(now - period + RETRY_AFTER_FAILURE))
        notify_event(session, "backup_failed", "Model Hub: backup failed", f"The scheduled backup could not be made ({e.__class__.__name__}).")
        return False
    set_setting(session, "auto_backup_last", str(now))
    return True
