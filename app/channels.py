"""More ways to be told than one webhook: Telegram, Pushover, Gotify, Matrix and Bark, each used when it is set up in Settings.

Every message has a level: *silent* (a print started or passed a step: no sound), *normal*, or *alarm* (a failure, a failed backup: always alerts).
The channels turn that into what they understand (Telegram's silent flag, Pushover/Gotify priority, Bark's interruption level). A channel that is down never stops the
others or the job that wanted to tell you. Secrets (tokens, keys) live only in Settings and are left out of backups."""
import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlmodel import Session

from app.settings_store import get_setting

logger = logging.getLogger("modelhub.channels")

LEVELS = ("silent", "normal", "alarm")
TELEGRAM_API = "https://api.telegram.org"
PUSHOVER_API = "https://api.pushover.net/1/messages.json"
BARK_DEFAULT = "https://api.day.app"
CHANNELS = ("telegram", "pushover", "gotify", "matrix", "bark")
SECRET_SETTINGS = ("telegram_token", "pushover_token", "pushover_user", "gotify_token", "matrix_token", "bark_key")


def _s(session: Session, key: str) -> str:
    return (get_setting(session, key, "") or "").strip()


def _url(value: str) -> Optional[str]:
    """An http(s) address with no user name or password, without a trailing slash; None when it is not usable."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        return None
    return value.rstrip("/")


def configured(session: Session) -> list:
    out = []
    if _s(session, "telegram_token") and _s(session, "telegram_chat_id"):
        out.append("telegram")
    if _s(session, "pushover_token") and _s(session, "pushover_user"):
        out.append("pushover")
    if _url(_s(session, "gotify_url")) and _s(session, "gotify_token"):
        out.append("gotify")
    if _url(_s(session, "matrix_url")) and _s(session, "matrix_token") and _s(session, "matrix_room"):
        out.append("matrix")
    if _s(session, "bark_key"):
        out.append("bark")
    return out


def telegram_chats(session: Session) -> list:
    return [c for c in re.split(r"[\s,;]+", _s(session, "telegram_chat_id")) if re.fullmatch(r"-?\d{1,20}", c)]


def _telegram(session: Session, title: str, message: str, image: Optional[bytes], level: str) -> bool:
    base = f"{TELEGRAM_API}/bot{_s(session, 'telegram_token')}"
    text = f"<b>{_esc(title)}</b>\n{_esc(message)}"[:3900]
    topic = _s(session, "telegram_topic")
    sent = False
    for chat in telegram_chats(session):
        extra = {"chat_id": chat, "disable_notification": "true" if level == "silent" else "false", "parse_mode": "HTML"}
        if topic.isdigit():
            extra["message_thread_id"] = topic
        try:
            if image:
                r = httpx.post(f"{base}/sendPhoto", data={**extra, "caption": text[:1000]}, files={"photo": ("snapshot.jpg", image, "image/jpeg")}, timeout=20)
            else:
                r = httpx.post(f"{base}/sendMessage", data={**extra, "text": text}, timeout=15)
            sent = sent or r.status_code == 200
        except Exception as e:
            logger.warning("Telegram message failed: %s", e.__class__.__name__)
    return sent


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pushover(session: Session, title: str, message: str, image: Optional[bytes], level: str) -> bool:
    data = {"token": _s(session, "pushover_token"), "user": _s(session, "pushover_user"), "title": title[:250], "message": message[:1024] or "-",
            "priority": {"silent": -1, "normal": 0, "alarm": 1}[level]}
    files = {"attachment": ("snapshot.jpg", image, "image/jpeg")} if image else None
    try:
        return httpx.post(PUSHOVER_API, data=data, files=files, timeout=20).status_code == 200
    except Exception as e:
        logger.warning("Pushover message failed: %s", e.__class__.__name__)
        return False


def _gotify(session: Session, title: str, message: str, image: Optional[bytes], level: str) -> bool:
    url = _url(_s(session, "gotify_url"))
    try:
        r = httpx.post(f"{url}/message", params={"token": _s(session, "gotify_token")}, json={"title": title[:200], "message": message or "-",
                       "priority": {"silent": 1, "normal": 5, "alarm": 8}[level]}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        logger.warning("Gotify message failed: %s", e.__class__.__name__)
        return False


def _matrix(session: Session, title: str, message: str, image: Optional[bytes], level: str) -> bool:
    import time
    url, room = _url(_s(session, "matrix_url")), _s(session, "matrix_room")
    body = {"msgtype": "m.notice" if level == "silent" else "m.text", "body": f"{title}\n{message}"}
    try:
        txn = f"modelhub{int(time.time() * 1000)}"
        r = httpx.put(f"{url}/_matrix/client/v3/rooms/{room.replace('#', '%23').replace(':', '%3A').replace('!', '%21')}/send/m.room.message/{txn}",
                      headers={"Authorization": f"Bearer {_s(session, 'matrix_token')}"}, json=body, timeout=15)
        return r.status_code == 200
    except Exception as e:
        logger.warning("Matrix message failed: %s", e.__class__.__name__)
        return False


def _bark(session: Session, title: str, message: str, image: Optional[bytes], level: str) -> bool:
    server = _url(_s(session, "bark_server")) or BARK_DEFAULT
    body = {"device_key": _s(session, "bark_key"), "title": title[:200], "body": message or "-", "group": "Model Hub",
            "level": {"silent": "passive", "normal": "active", "alarm": "timeSensitive"}[level]}
    try:
        return httpx.post(f"{server}/push", json=body, timeout=15).status_code == 200
    except Exception as e:
        logger.warning("Bark message failed: %s", e.__class__.__name__)
        return False


SENDERS = {"telegram": _telegram, "pushover": _pushover, "gotify": _gotify, "matrix": _matrix, "bark": _bark}


def send_all(session: Session, title: str, message: str, image: Optional[bytes] = None, level: str = "normal") -> list:
    """Send to every channel that is set up. Returns the names of those that took it."""
    level = level if level in LEVELS else "normal"
    done = []
    for name in configured(session):
        try:
            if SENDERS[name](session, title, message, image, level):
                done.append(name)
        except Exception as e:                              # one channel misbehaving must not stop the rest
            logger.warning("%s failed: %s", name, e.__class__.__name__)
    return done
