"""Looking (at most once a day, and only if switched on) for a newer Model Hub release on GitHub.

Only the public release information is requested; nothing about your library or server is sent.
"""
import logging
import time
from typing import Optional

import httpx
from sqlmodel import Session

from app import version
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

logger = logging.getLogger("modelhub.updates")

RELEASES_API = "https://api.github.com/repos/aon082910/model-hub/releases/latest"
RELEASES_PAGE = "https://github.com/aon082910/model-hub/releases"


class UpdateCheckError(Exception):
    pass


def fetch_latest() -> dict:
    """{"tag": "v2.9.0", "url": ...} for the newest release."""
    try:
        r = httpx.get(RELEASES_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "ModelHub-update-check"},
                      timeout=10, follow_redirects=False)
    except httpx.HTTPError as e:
        raise UpdateCheckError(f"Could not reach GitHub ({e.__class__.__name__})")
    if r.status_code == 403 or r.status_code == 429:
        raise UpdateCheckError("GitHub is rate limiting requests; try again later")
    if r.status_code != 200:
        raise UpdateCheckError(f"GitHub answered with an error ({r.status_code})")
    try:
        data = r.json()
        tag = str(data["tag_name"])
    except (ValueError, KeyError, TypeError):
        raise UpdateCheckError("GitHub returned something unexpected")
    if version.parse(tag) is None:
        raise UpdateCheckError("The newest release has an unexpected name")
    url = data.get("html_url") if isinstance(data.get("html_url"), str) and data["html_url"].startswith("https://github.com/") else RELEASES_PAGE
    return {"tag": tag, "url": url}


def enabled(session: Session) -> bool:
    return get_setting(session, "update_check", "true") != "false"


def check(session: Session, force: bool = False) -> dict:
    """Ask GitHub (when switched on, or forced), remember the answer, and notify once per new version."""
    if not force and not enabled(session):
        return status(session)
    latest = fetch_latest()
    set_setting(session, "update_latest", latest["tag"])
    set_setting(session, "update_url", latest["url"])
    set_setting(session, "update_checked_at", str(int(time.time())))
    if version.is_newer(latest["tag"]) and get_setting(session, "update_notified", "") != latest["tag"]:
        set_setting(session, "update_notified", latest["tag"])
        notify_event(session, "update_available", "Model Hub: update available",
                     f"{latest['tag']} is out (you have {version.VERSION}). {latest['url']}")
    return status(session)


def scheduled(session: Session) -> None:
    try:
        check(session)
    except UpdateCheckError as e:
        logger.info("Update check failed: %s", e)


def status(session: Session) -> dict:
    latest = get_setting(session, "update_latest", "") or None
    try:
        checked = int(get_setting(session, "update_checked_at", "") or 0) or None
    except ValueError:
        checked = None
    return {"current": version.VERSION, "latest": latest, "update_available": version.is_newer(latest),
            "url": get_setting(session, "update_url", "") or RELEASES_PAGE, "checked_at": checked, "enabled": enabled(session)}
