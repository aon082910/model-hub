"""Maintenance for printers: tasks that fall due after so many hours of printing and/or so many days.

A printer's print hours are the minutes of every print log entry a printer reported for it (failed ones too: the machine ran),
so printers that Model Hub is not connected to only get the day-based part right.
"""
import json
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import MaintenanceTask, Printer, PrintLog
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

SOON = 0.9                 # from this share of the interval on, a task is "soon"
PRESETS = [                 # offered when adding a task
    {"name": "Clean and lubricate the rails or rods", "every_hours": 200},
    {"name": "Check the belt tension", "every_hours": 300},
    {"name": "Clean the extruder gears", "every_hours": 150},
    {"name": "Replace the nozzle", "every_hours": 500},
    {"name": "Replace the PTFE tube", "every_hours": 1000},
    {"name": "Clean the fans and the enclosure", "every_days": 90},
    {"name": "Check the screws and the bed level", "every_days": 60},
]


def print_hours(session: Session, printer_id: int) -> float:
    minutes = session.exec(select(PrintLog.minutes).where(PrintLog.printer_id == printer_id)).all()
    return round(sum(float(m) for m in minutes if m) / 60, 1)


def describe(session: Session, task: MaintenanceTask, names: dict, hours_cache: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    if task.printer_id not in hours_cache:
        hours_cache[task.printer_id] = print_hours(session, task.printer_id)
    hours_now = hours_cache[task.printer_id]
    hours_since = max(0.0, round(hours_now - (task.last_done_hours or 0), 1))
    days_since = max(0, (now - task.last_done_at).days)
    shares = []
    if task.every_hours:
        shares.append(hours_since / task.every_hours)
    if task.every_days:
        shares.append(days_since / task.every_days)
    share = max(shares) if shares else 0
    status = "due" if share >= 1 else "soon" if share >= SOON else "ok"
    return {"id": task.id, "printer_id": task.printer_id, "printer": names.get(task.printer_id), "name": task.name,
            "every_hours": task.every_hours, "every_days": task.every_days, "note": task.note,
            "last_done_at": task.last_done_at.isoformat(), "hours_since": hours_since, "days_since": days_since,
            "hours_left": round(task.every_hours - hours_since, 1) if task.every_hours else None,
            "days_left": task.every_days - days_since if task.every_days else None,
            "status": status, "share": round(share, 2)}


def overview(session: Session) -> list:
    names = {p.id: p.name for p in session.exec(select(Printer)).all()}
    cache: dict = {}
    rows = [describe(session, t, names, cache) for t in session.exec(select(MaintenanceTask).order_by(MaintenanceTask.id)).all() if t.printer_id in names]
    return sorted(rows, key=lambda r: (-r["share"], r["id"]))


def forget_printer(session: Session, printer_id: int) -> None:
    for task in session.exec(select(MaintenanceTask).where(MaintenanceTask.printer_id == printer_id)).all():
        session.delete(task)


def check_and_notify(session: Session) -> list:
    """Notify once about each task that became due; it is forgotten when it is done and may be announced again later."""
    due = {r["id"]: f"{r['printer']}: {r['name']}" for r in overview(session) if r["status"] == "due"}
    try:
        before = set(json.loads(get_setting(session, "maintenance_notified", "[]")))
    except ValueError:
        before = set()
    fresh = [text for key, text in due.items() if key not in before]
    set_setting(session, "maintenance_notified", json.dumps(sorted(due)))
    if fresh:
        shown = "; ".join(fresh[:6]) + (f" and {len(fresh) - 6} more" if len(fresh) > 6 else "")
        notify_event(session, "maintenance_due", "Model Hub: printer maintenance due", shown)
    return fresh
