from datetime import datetime

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlmodel import Session, select

from app import activity, maintenance
from app.db import get_session
from app.models import MaintenanceLog, MaintenanceTask, Printer

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])
MAX_TASKS = 200


def _number(value, name: str, low: float, high: float):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
        raise HTTPException(400, f"{name} must be a number between {low:g} and {high:g}")
    return value


@router.get("")
def list_tasks(session: Session = Depends(get_session)):
    from app.models import Printer as PrinterRow
    names = [{"id": p.id, "name": p.name} for p in session.exec(select(PrinterRow).order_by(PrinterRow.name)).all()]
    return {"tasks": maintenance.overview(session), "presets": maintenance.PRESETS, "printers": names}


@router.post("")
def add_task(payload: dict, request: Request, session: Session = Depends(get_session)):
    printer = session.get(Printer, payload.get("printer_id")) if isinstance(payload.get("printer_id"), int) else None
    if not printer:
        raise HTTPException(400, "Choose the printer")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "Say what has to be done")
    hours = _number(payload.get("every_hours"), "every_hours", 1, 100000)
    days = _number(payload.get("every_days"), "every_days", 1, 3650)
    if not hours and not days:
        raise HTTPException(400, "Give the number of print hours and/or days between times")
    if len(session.exec(select(MaintenanceTask.id)).all()) >= MAX_TASKS:
        raise HTTPException(400, f"At most {MAX_TASKS} maintenance tasks")
    note = payload.get("note")
    task = MaintenanceTask(printer_id=printer.id, name=name.strip()[:80], every_hours=hours, every_days=int(days) if days else None,
                           last_done_hours=maintenance.print_hours(session, printer.id), note=note.strip()[:300] if isinstance(note, str) and note.strip() else None)
    session.add(task)
    session.commit()
    session.refresh(task)
    activity.record(session, activity.actor_of(request), "maintenance", f"Added the maintenance task \"{task.name}\" for {printer.name}")
    return next(r for r in maintenance.overview(session) if r["id"] == task.id)


@router.get("/log")
def history(printer_id: Optional[int] = None, limit: int = 100, session: Session = Depends(get_session)):
    """What was done, newest first (all printers, or one)."""
    stmt = select(MaintenanceLog).order_by(MaintenanceLog.done_at.desc(), MaintenanceLog.id.desc())
    if printer_id is not None:
        stmt = stmt.where(MaintenanceLog.printer_id == printer_id)
    names = {p.id: p.name for p in session.exec(select(Printer)).all()}
    rows = session.exec(stmt.limit(max(1, min(limit, 500)))).all()
    return {"log": [{"id": r.id, "printer_id": r.printer_id, "printer": names.get(r.printer_id), "name": r.name, "done_at": r.done_at.isoformat(),
                     "print_hours": r.print_hours, "note": r.note} for r in rows if r.printer_id in names]}


@router.delete("/log/{log_id}")
def delete_log(log_id: int, session: Session = Depends(get_session)):
    row = session.get(MaintenanceLog, log_id)
    if not row:
        raise HTTPException(404, "Not found")
    session.delete(row)
    session.commit()
    return {"status": "deleted"}


@router.post("/{task_id}/done")
def mark_done(task_id: int, request: Request, payload: Optional[dict] = Body(default=None), session: Session = Depends(get_session)):
    """Say it was done now: the clock starts again from this moment and from the printer's print hours so far. An optional note is kept in the history."""
    task = session.get(MaintenanceTask, task_id)
    if not task:
        raise HTTPException(404, "Not found")
    note = (payload or {}).get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(400, "note must be text")
    task.last_done_at = datetime.utcnow()
    task.last_done_hours = maintenance.print_hours(session, task.printer_id)
    session.add(task)
    session.add(MaintenanceLog(printer_id=task.printer_id, task_id=task.id, name=task.name, print_hours=task.last_done_hours,
                               note=note.strip()[:300] if note and note.strip() else None))
    session.commit()
    printer = session.get(Printer, task.printer_id)
    activity.record(session, activity.actor_of(request), "maintenance", f"Maintenance done: {task.name} on {printer.name if printer else 'a printer'}")
    return next(r for r in maintenance.overview(session) if r["id"] == task_id)


@router.patch("/{task_id}")
def edit_task(task_id: int, payload: dict, session: Session = Depends(get_session)):
    task = session.get(MaintenanceTask, task_id)
    if not task:
        raise HTTPException(404, "Not found")
    if "name" in payload:
        if not isinstance(payload["name"], str) or not payload["name"].strip():
            raise HTTPException(400, "Say what has to be done")
        task.name = payload["name"].strip()[:80]
    if "every_hours" in payload:
        task.every_hours = _number(payload["every_hours"], "every_hours", 1, 100000)
    if "every_days" in payload:
        days = _number(payload["every_days"], "every_days", 1, 3650)
        task.every_days = int(days) if days else None
    if not task.every_hours and not task.every_days:
        raise HTTPException(400, "Give the number of print hours and/or days between times")
    if "note" in payload:
        task.note = payload["note"].strip()[:300] if isinstance(payload["note"], str) and payload["note"].strip() else None
    session.add(task)
    session.commit()
    return next(r for r in maintenance.overview(session) if r["id"] == task_id)


@router.delete("/{task_id}")
def delete_task(task_id: int, session: Session = Depends(get_session)):
    task = session.get(MaintenanceTask, task_id)
    if not task:
        raise HTTPException(404, "Not found")
    session.delete(task)
    session.commit()
    return {"status": "deleted"}
