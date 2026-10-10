"""Quiet hours and a daily digest for notifications.

* **Quiet hours** (Settings, Notifications: from and to, like 22:00 and 07:00): ordinary messages are not sent during them but kept, and arrive together as one message when the quiet hours end.
* **Daily digest** (a time, like 08:00): ordinary messages are kept all day and arrive together once a day at that time.
Alarms (a suspected failed print, a failed backup) always go straight through. The times are the server's own clock (set the container's TZ for your zone). Held messages are plain
text (a camera picture is not kept), at most 200 are held, and a held message is lost only if the container is removed before the digest is sent."""
import re
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import HeldNotice
from app.settings_store import get_setting, set_setting

MAX_HELD = 200
LINES_IN_DIGEST = 25


def _minutes(text: str) -> Optional[int]:
    m = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", text or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def valid_time(text: str) -> bool:
    return _minutes(text) is not None


def _now_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def quiet_hours(session: Session) -> Optional[tuple]:
    start, end = _minutes(get_setting(session, "quiet_start", "")), _minutes(get_setting(session, "quiet_end", ""))
    return (start, end) if start is not None and end is not None and start != end else None


def in_quiet(session: Session, now: Optional[datetime] = None) -> bool:
    window = quiet_hours(session)
    if not window:
        return False
    now_m = _now_minutes(now or datetime.now())
    start, end = window
    return start <= now_m < end if start < end else (now_m >= start or now_m < end)


def daily_time(session: Session) -> Optional[int]:
    if get_setting(session, "digest_daily", "") != "true":
        return None
    t = _minutes(get_setting(session, "digest_time", "") or "08:00")
    return 8 * 60 if t is None else t


def should_hold(session: Session, level: str, now: Optional[datetime] = None) -> bool:
    if level == "alarm":
        return False
    return daily_time(session) is not None or in_quiet(session, now)


def hold(session: Session, title: str, message: str, level: str, event: Optional[str]) -> None:
    session.add(HeldNotice(title=title[:200], message=message[:1500], level=level[:10], event=(event or "")[:40] or None))
    session.commit()
    rows = session.exec(select(HeldNotice).order_by(HeldNotice.id)).all()
    for old in rows[:max(0, len(rows) - MAX_HELD)]:
        session.delete(old)
    session.commit()


def held(session: Session) -> list:
    return session.exec(select(HeldNotice).order_by(HeldNotice.id)).all()


def compose(rows: list) -> tuple:
    lines = [f"{r.at:%H:%M} {r.title.replace('Model Hub: ', '', 1)}: {r.message}".replace("\n", " ") for r in rows[:LINES_IN_DIGEST]]
    if len(rows) > LINES_IN_DIGEST:
        lines.append(f"... and {len(rows) - LINES_IN_DIGEST} more")
    return f"Model Hub: {len(rows)} message{'s' if len(rows) != 1 else ''} while you were away", "\n".join(lines)


def release_due(session: Session, now: Optional[datetime] = None) -> int:
    """Send the held messages as one, if it is time. Returns how many were in it."""
    now = now or datetime.now()
    rows = held(session)
    if not rows:
        return 0
    daily = daily_time(session)
    today = now.strftime("%Y-%m-%d")
    if daily is not None:
        cutoff = now.replace(hour=daily // 60, minute=daily % 60, second=0, microsecond=0)
        if now < cutoff or get_setting(session, "digest_last", "") == today or not any(r.at < cutoff for r in rows):
            return 0                               # (a message held after today's time waits for tomorrow's)
    elif in_quiet(session, now):
        return 0
    title, message = compose(rows)
    from app.notify import notify
    notify(session, title, message, level="normal", hold=False)
    for r in rows:
        session.delete(r)
    set_setting(session, "digest_last", today)
    session.commit()
    return len(rows)


def scheduled(session: Session) -> int:
    return release_due(session)
