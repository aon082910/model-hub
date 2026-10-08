from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session

from app import activity, calendar_plan
from app.db import get_session
from app.models import QueueItem

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


@router.get("")
def month(month: str = "", printer: str = "", session: Session = Depends(get_session)):
    """One month: planned prints and logged prints per day, and the waiting entries that have no day yet.
    printer limits it to one printer's prints (its id), or "none" for entries without a printer."""
    try:
        return calendar_plan.month_view(session, month or date.today().strftime("%Y-%m"), calendar_plan.parse_printer(printer))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/plan")
def plan(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Give waiting entries a day (the first with room, in queue order). dry_run=true only shows what would happen.
    A real run can be undone from Recent changes."""
    start = None
    if payload.get("start"):
        try:
            start = date.fromisoformat(str(payload["start"]))
        except ValueError:
            raise HTTPException(400, "The start date must look like 2026-10-31")
    dry = payload.get("dry_run") is True
    result = calendar_plan.auto_plan(session, start, dry_run=dry)
    activity_id = None
    if not dry and result["assigned"]:
        new = {str(a["id"]): a["planned_date"] for a in result["assigned"]}
        entry = activity.record(session, activity.actor_of(request), "calendar", f"Planned {len(new)} print(s) into the calendar",
                                undo={"kind": "planned_dates", "new": new})
        activity_id = entry.id
    return {**result, "dry_run": dry, "activity_id": activity_id}


@router.get("/export.ics")
def export_ics(printer: str = "", session: Session = Depends(get_session)):
    try:
        wanted = calendar_plan.parse_printer(printer)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(calendar_plan.ics(session, wanted), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="modelhub-prints.ics"'})
