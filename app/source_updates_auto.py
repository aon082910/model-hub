"""An optional weekly-ish look at linked listings (Settings: check listings automatically)."""
from sqlmodel import Session

from app import source_updates
from app.settings_store import get_setting


def maybe_start(session: Session) -> bool:
    if get_setting(session, "auto_listing_check", "false") != "true":
        return False
    try:
        source_updates.start_job(force=False, notify_changes=True)
    except source_updates.JobBusy:
        return False
    return True
