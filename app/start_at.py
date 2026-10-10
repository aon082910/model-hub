"""Start a queued print by itself at a chosen time.

Off until Settings says so ("let Model Hub start prints at their scheduled time"). A queue entry with a printer and a start time is sent and started when the time comes, using the very same
checks as pressing *Send and start* by hand and none of the "start anyway" shortcuts: the printer must answer and be idle, its plate must be clear (when that wait is on), it must not be on hold
or out of service, no stagger or power limit may say wait, the spool must not be low, the colour must fit (when asked for), the budget must allow it. If any check says wait, the entry keeps
waiting and retrying; you are told once, and after two hours the start is given up (the entry stays in the queue). Nothing is ever started without a time you set."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app import activity
from app.models import Printer, QueueItem
from app.notify import notify_event
from app.settings_store import get_setting

logger = logging.getLogger("modelhub.start_at")
GRACE = timedelta(hours=2)
PAST_OK = timedelta(minutes=5)
_told: set = set()                      # queue entries already told about as blocked


def enabled(session: Session) -> bool:
    return get_setting(session, "scheduled_starts", "") == "true"


def clean(value) -> Optional[datetime]:
    """A start time as a UTC datetime (a naive datetime is taken as UTC), or None to clear it. Raises HTTPException for nonsense."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise HTTPException(400, "The start time must look like 2026-10-31T18:30:00Z")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "The start time must look like 2026-10-31T18:30:00Z")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    if parsed < now - PAST_OK:
        raise HTTPException(400, "That time has already passed")
    if parsed > now + timedelta(days=365):
        raise HTTPException(400, "A start time at most a year ahead")
    return parsed.replace(microsecond=0)


def reset() -> None:
    _told.clear()


def scheduled(session: Session, now: Optional[datetime] = None) -> list:
    """Try to start every entry whose time has come. Returns the names of the prints that were started."""
    if not enabled(session):
        return []
    from app.routers.queue import send_item
    from app.models import Model3D
    now = now or datetime.utcnow()
    started = []
    due = session.exec(select(QueueItem).where(QueueItem.status == "queued", QueueItem.start_at.is_not(None), QueueItem.start_at <= now).order_by(QueueItem.start_at)).all()
    for item in due:
        printer = session.get(Printer, item.printer_id) if item.printer_id else None
        model = session.get(Model3D, item.model_id)
        label = model.filename if model else f"model {item.model_id}"
        try:
            if not printer:
                raise HTTPException(400, "No printer is chosen for it")
            send_item(item.id, {"start": True}, None, session)
        except HTTPException as e:
            session.rollback()
            item = session.get(QueueItem, item.id)
            reason = str(e.detail)
            if now - item.start_at > GRACE:
                item.start_at, item.start_note = None, f"The scheduled start was given up: {reason}"
                notify_event(session, "schedule_blocked", f"Model Hub: a scheduled print was not started", f"{label}: gave up after two hours of waiting. {reason}")
                _told.discard(item.id)
            else:
                item.start_note = f"Waiting to start: {reason}"
                if item.id not in _told:
                    _told.add(item.id)
                    notify_event(session, "schedule_blocked", f"Model Hub: a scheduled print is waiting", f"{label} should have started on {printer.name if printer else 'a printer'} but: {reason}. It will keep trying for two hours.")
            session.add(item)
            session.commit()
            continue
        except Exception as e:
            session.rollback()
            logger.warning("Scheduled start of %s failed: %s", label, e.__class__.__name__)
            continue
        item = session.get(QueueItem, item.id)
        item.start_at, item.start_note = None, None
        session.add(item)
        session.commit()
        _told.discard(item.id)
        activity.record(session, "Model Hub", "queue", f"Started the scheduled print of {label} on {printer.name}")
        started.append(label)
    return started
