"""Generic outbound webhook notifications.

There is no portable way for a container to reach into the Unraid host and call
its native notify script -- that lives outside the container's namespace. The
practical, documented pattern instead: point this at ntfy.sh (free, has an
Unraid Community Apps plugin/app you can install to see pushes), a Discord/Slack
incoming webhook, or Unraid's own "webhook" User Script trigger.

Each kind of event can be switched off in Settings (setting notify_<event> = "false").
"""
import logging
import httpx
from sqlmodel import Session

from app.settings_store import get_setting

logger = logging.getLogger("modelhub.notify")

EVENTS = {
    "new_files": "New files found in the library",
    "tagging_done": "AI tagging finished",
    "new_uploads": "A designer you follow uploaded something",
    "listing_changes": "A linked listing changed",
    "low_stock": "A spool or supply is running low",
    "print_done": "A printer finished a print",
    "backup_failed": "A scheduled backup failed",
    "update_available": "A newer Model Hub release is out",
}


def event_enabled(session: Session, event: str) -> bool:
    return get_setting(session, f"notify_{event}", "true") != "false"


def notify_event(session: Session, event: str, title: str, message: str) -> bool:
    """Send a notification for a named event, unless that event is switched off. True if it was sent."""
    if event not in EVENTS or not event_enabled(session, event):
        return False
    return notify(session, title, message)


def notify(session: Session, title: str, message: str) -> bool:
    url = get_setting(session, "notify_webhook_url")
    if not url:
        return False
    try:
        # A generic JSON body covers Discord/Slack-style webhooks (which accept
        # "content"/"text") and ntfy (which reads the raw body as the message,
        # with title carried in a header) in one shot.
        httpx.post(
            url,
            json={"title": title, "message": message, "content": f"**{title}**\n{message}", "text": f"{title}: {message}"},
            headers={"Title": title[:200]},
            timeout=10,
        )
        return True
    except Exception as e:
        logger.warning("Notification webhook failed: %s", e)
        return False
