from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlmodel import Session, select

from app import activity
from app.db import get_session
from app.models import ActivityLog

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("")
def list_activity(actor: Optional[str] = None, action: Optional[str] = None, limit: int = Query(50, ge=1, le=200),
                  offset: int = Query(0, ge=0), session: Session = Depends(get_session)):
    stmt, count = select(ActivityLog), select(func.count()).select_from(ActivityLog)
    if actor:
        stmt, count = stmt.where(ActivityLog.actor == actor), count.where(ActivityLog.actor == actor)
    if action:
        stmt, count = stmt.where(ActivityLog.action == action), count.where(ActivityLog.action == action)
    rows = session.exec(stmt.order_by(ActivityLog.id.desc()).offset(offset).limit(limit)).all()
    actors = session.exec(select(ActivityLog.actor).distinct().order_by(ActivityLog.actor)).all()
    return {"total": session.exec(count).one(), "items": [activity.as_json(r) for r in rows], "actors": list(actors)}


@router.post("/{entry_id}/undo")
def undo_entry(entry_id: int, request: Request, session: Session = Depends(get_session)):
    entry = session.get(ActivityLog, entry_id)
    if not entry:
        raise HTTPException(404, "Not found")
    try:
        restored = activity.undo(session, entry, activity.actor_of(request))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"restored": restored}
