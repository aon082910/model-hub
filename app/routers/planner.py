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


@router.post("/copy")
def copy_week(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Repeat the week that contains `date` (default: this week) for the next `weeks` weeks (default 1). dry_run=true only counts.
    A real run can be undone from Recent changes."""
    try:
        day = date.fromisoformat(str(payload.get("date") or date.today().isoformat()))
    except ValueError:
        raise HTTPException(400, "The date must look like 2026-10-31")
    weeks = payload.get("weeks", 1)
    if not isinstance(weeks, int) or isinstance(weeks, bool):
        raise HTTPException(400, "weeks must be a number")
    statuses = payload.get("statuses")
    if statuses is None:
        statuses = ["queued", "printing", "done"]
    if not isinstance(statuses, list):
        raise HTTPException(400, "statuses must be a list")
    dry = payload.get("dry_run") is True
    try:
        result = calendar_plan.copy_weeks(session, day, weeks, statuses, dry_run=dry)
    except ValueError as e:
        raise HTTPException(400, str(e))
    activity_id = None
    if not dry and result["created"]:
        entry = activity.record(session, activity.actor_of(request), "calendar",
                                f"Repeated the week of {result['week']} for {weeks} more week(s): {len(result['created'])} print(s) added",
                                undo={"kind": "created_queue", "ids": [c["id"] for c in result["created"]]})
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
