"""Generic outbound webhook notifications.

There is no portable way for a container to reach into the Unraid host and call
its native notify script -- that lives outside the container's namespace. The
practical, documented pattern instead: point this at ntfy.sh (free, has an
Unraid Community Apps plugin/app you can install to see pushes), a Discord/Slack
incoming webhook, or Unraid's own "webhook" User Script trigger.

Each kind of event can be switched off in Settings (setting notify_<event> = "false").
"""
import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

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
    "plan_short": "A planned print needs more filament than you have",
    "maintenance_due": "A printer needs maintenance",
    "failure_suspected": "A camera thinks a print may have failed",
    "dry_due": "A spool in a printer is due for drying",
    "stock_low": "Finished parts on the shelf are at or below their minimum",
    "print_started": "A print started (off until you switch it on)",
    "print_progress": "A print passed another step of its progress (off until you switch it on)",
    "print_paused": "A print was paused or resumed (off until you switch it on)",
    "weekly_summary": "The weekly summary (only sent if you switch it on)",
    "print_done": "A printer finished a print",
    "backup_failed": "A scheduled backup failed",
    "update_available": "A newer Model Hub release is out",
}


DEFAULT_OFF = {"print_started", "print_progress", "print_paused"}            # these would be noisy: you switch them on
DISCORD_HOSTS = ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com")
PLACEHOLDERS = ("printer", "file", "progress", "minutes")


def event_enabled(session: Session, event: str) -> bool:
    value = get_setting(session, f"notify_{event}", "")
    return value == "true" if event in DEFAULT_OFF else value != "false"


def is_discord(url: Optional[str]) -> bool:
    try:
        return (urlparse(url or "").hostname or "") in DISCORD_HOSTS
    except ValueError:
        return False


def apply_template(session: Session, event: str, message: str, fields: Optional[dict]) -> str:
    """Your own wording for an event ({printer}, {file}, {progress}, {minutes} are filled in; nothing else is evaluated)."""
    text = get_setting(session, f"notify_text_{event}", "") or ""
    if not text.strip() or not fields:
        return message
    return re.sub(r"\{(\w+)\}", lambda m: str(fields.get(m.group(1), m.group(0))), text.strip())[:1500]


def notify_event(session: Session, event: str, title: str, message: str, fields: Optional[dict] = None, image: Optional[bytes] = None) -> bool:
    """Send a notification for a named event, unless that event is switched off. True if it was sent.
    fields fill in the placeholders of a custom text; image (a JPEG) is attached when the webhook is a Discord one."""
    if event not in EVENTS or not event_enabled(session, event):
        return False
    return notify(session, title, apply_template(session, event, message, fields), image)


def notify(session: Session, title: str, message: str, image: Optional[bytes] = None) -> bool:
    url = get_setting(session, "notify_webhook_url")
    if not url:
        return False
    if image and is_discord(url):
        try:
            payload = json.dumps({"content": f"**{title}**\n{message}"[:1900]})
            httpx.post(url, data={"payload_json": payload}, files={"files[0]": ("snapshot.jpg", image, "image/jpeg")}, timeout=15)
            return True
        except Exception as e:
            logger.warning("Notification webhook (with picture) failed: %s", e)
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
