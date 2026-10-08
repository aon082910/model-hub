"""A record of who changed what, with undo for bulk edits.

Only meaningful changes are recorded (bulk edits, removals from the library, duplicate clean-ups, restores, logins and
tokens, share links, printers, which setting names changed): never passwords, tokens, keys or setting values. The newest
2000 entries are kept.
"""
import json
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import ActivityLog

MAX_ENTRIES = 2000


def actor_of(request) -> str:
    user = getattr(getattr(request, "state", None), "user", None) or {}
    return user.get("username") or "system"


def record(session: Session, actor: str, action: str, summary: str, undo: Optional[dict] = None) -> ActivityLog:
    entry = ActivityLog(actor=(actor or "system")[:80], action=action[:40], summary=summary[:500],
                        undo_json=json.dumps(undo) if undo else None)
    session.add(entry)
    session.commit()
    session.refresh(entry)
    count = len(session.exec(select(ActivityLog.id)).all())
    if count > MAX_ENTRIES + 100:                                   # trim in batches, not on every write
        for old in session.exec(select(ActivityLog).order_by(ActivityLog.id).limit(count - MAX_ENTRIES)).all():
            session.delete(old)
        session.commit()
    return entry


def as_json(entry: ActivityLog) -> dict:
    return {"id": entry.id, "at": entry.at, "actor": entry.actor, "action": entry.action, "summary": entry.summary,
            "undoable": bool(entry.undo_json) and not entry.undone, "undone": entry.undone,
            "undone_at": entry.undone_at, "undone_by": entry.undone_by}


def _unplan(session: Session, planned: dict) -> int:
    """Take planned days away again, but only from entries still on the day the plan gave them."""
    from app.models import QueueItem
    count = 0
    for item_id, day in planned.items():
        item = session.get(QueueItem, int(item_id)) if str(item_id).isdigit() else None
        if item and item.planned_date == day:
            item.planned_date = None
            session.add(item)
            count += 1
    session.commit()
    return count


def _remove_created(session: Session, ids: list) -> int:
    """Take away queue entries that were added in bulk, but only ones still waiting (a print that has begun stays)."""
    from app.models import QueueItem
    count = 0
    for item_id in ids:
        item = session.get(QueueItem, item_id) if isinstance(item_id, int) else None
        if item and item.status == "queued":
            session.delete(item)
            count += 1
    session.commit()
    return count


def undo(session: Session, entry: ActivityLog, actor: str) -> int:
    """Reverse an entry. Raises ValueError if it cannot be (or was already)."""
    if entry.undone:
        raise ValueError("That change was already undone")
    if not entry.undo_json:
        raise ValueError("That change cannot be undone from here")
    spec = json.loads(entry.undo_json)
    from app.routers import bulk
    if spec.get("kind") == "bulk":
        restored = bulk.undo(session, spec)
    elif spec.get("kind") == "multi":
        restored = sum(bulk.undo(session, one) for one in spec.get("specs", []) if isinstance(one, dict) and one.get("kind") == "bulk")
    elif spec.get("kind") == "created_queue":
        restored = _remove_created(session, spec.get("ids") or [])
    elif spec.get("kind") == "planned_dates":
        restored = _unplan(session, spec.get("new") or {})
    else:
        raise ValueError("That change cannot be undone from here")
    entry.undone, entry.undone_at, entry.undone_by = True, datetime.utcnow(), actor
    session.add(entry)
    session.commit()
    record(session, actor, "undo", f"Undid: {entry.summary} ({restored} put back)")
    return restored
