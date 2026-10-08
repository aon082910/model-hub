from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app import activity, maintenance
from app.db import get_session
from app.models import MaintenanceTask, Printer

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


@router.post("/{task_id}/done")
def mark_done(task_id: int, request: Request, session: Session = Depends(get_session)):
    """Say it was done now: the clock starts again from this moment and from the printer's print hours so far."""
    task = session.get(MaintenanceTask, task_id)
    if not task:
        raise HTTPException(404, "Not found")
    task.last_done_at = datetime.utcnow()
    task.last_done_hours = maintenance.print_hours(session, task.printer_id)
    session.add(task)
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
