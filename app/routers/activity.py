import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import func
from sqlmodel import Session, select

from app import activity
from app.db import get_session
from app.models import ActivityLog

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("")
def list_activity(request: Request, actor: Optional[str] = None, action: Optional[str] = None, limit: int = Query(50, ge=1, le=200),
                  offset: int = Query(0, ge=0), session: Session = Depends(get_session)):
    stmt, count = select(ActivityLog), select(func.count()).select_from(ActivityLog)
    if (getattr(request.state, "user", None) or {}).get("role") != "admin":
        stmt, count = stmt.where(ActivityLog.action != "security"), count.where(ActivityLog.action != "security")     # sign-in events are the administrator's
    if actor:
        stmt, count = stmt.where(ActivityLog.actor == actor), count.where(ActivityLog.actor == actor)
    if action:
        stmt, count = stmt.where(ActivityLog.action == action), count.where(ActivityLog.action == action)
    rows = session.exec(stmt.order_by(ActivityLog.id.desc()).offset(offset).limit(limit)).all()
    actors = session.exec(select(ActivityLog.actor).distinct().order_by(ActivityLog.actor)).all()
    return {"total": session.exec(count).one(), "items": [activity.as_json(r) for r in rows], "actors": list(actors)}


@router.get("/export.csv")
def export_csv(actor: Optional[str] = None, action: Optional[str] = None, session: Session = Depends(get_session)):
    """The activity record as a spreadsheet (the newest 20 000 entries, oldest first), for keeping or checking elsewhere. Administrator only."""
    def safe(value):
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
    stmt = select(ActivityLog)
    if actor:
        stmt = stmt.where(ActivityLog.actor == actor)
    if action:
        stmt = stmt.where(ActivityLog.action == action)
    rows = list(reversed(session.exec(stmt.order_by(ActivityLog.id.desc()).limit(20000)).all()))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["when (UTC)", "who", "kind", "what"])
    for r in rows:
        writer.writerow([r.at.isoformat() if r.at else "", safe(r.actor), safe(r.action), safe(r.summary)])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="modelhub-activity.csv"'})


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
