"""Maintenance for printers: tasks that fall due after so many hours of printing and/or so many days.

A printer's print hours are the minutes of every print log entry a printer reported for it (failed ones too: the machine ran),
so printers that Model Hub is not connected to only get the day-based part right.
"""
import json
import re
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import MaintenanceLog, MaintenanceTask, Printer, PrintLog
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


def usage_since(session: Session, task: MaintenanceTask) -> dict:
    """What the printer has done since the task was last done: grams of filament, finished prints, failed prints (of the task's reason, if it names one)."""
    from app import print_outcomes
    logs = session.exec(select(PrintLog).where(PrintLog.printer_id == task.printer_id, PrintLog.printed_at >= task.last_done_at)).all()
    ok = [l for l in logs if not print_outcomes.is_failed(l)]
    failed = [l for l in logs if print_outcomes.is_failed(l) and (not task.failure_reason or l.failure_reason == task.failure_reason)]
    return {"grams": round(sum(float(l.grams or 0) for l in logs), 1), "prints": len(ok), "failures": len(failed)}


def daily_rates(session: Session, printer_id: int, now: datetime, days: int = 30) -> dict:
    """How much the printer is used per day, from the last 30 days (a moving average): print hours, grams, finished prints, failed prints."""
    from datetime import timedelta
    from app import print_outcomes
    logs = session.exec(select(PrintLog).where(PrintLog.printer_id == printer_id, PrintLog.printed_at >= now - timedelta(days=days))).all()
    failed = [l for l in logs if print_outcomes.is_failed(l)]
    return {"hours": sum(float(l.minutes or 0) for l in logs) / 60 / days, "grams": sum(float(l.grams or 0) for l in logs) / days,
            "prints": (len(logs) - len(failed)) / days, "failures": len(failed) / days}


def advance_share() -> float:
    return SOON


def describe(session: Session, task: MaintenanceTask, names: dict, hours_cache: dict, now: Optional[datetime] = None) -> dict:
    from datetime import timedelta
    now = now or datetime.utcnow()
    if task.printer_id not in hours_cache:
        hours_cache[task.printer_id] = print_hours(session, task.printer_id)
    hours_now = hours_cache[task.printer_id]
    hours_since = max(0.0, round(hours_now - (task.last_done_hours or 0), 1))
    days_since = max(0, (now - task.last_done_at).days)
    used = usage_since(session, task)
    rates = daily_rates(session, task.printer_id, now)
    shares, days_left_by = [], []            # a trigger's share of its interval used, and in how many days it will be reached at the recent rate
    for every, since, rate in ((task.every_hours, hours_since, rates["hours"]), (task.every_grams, used["grams"], rates["grams"]),
                               (task.every_prints, used["prints"], rates["prints"]), (task.every_failures, used["failures"], rates["failures"])):
        if every:
            shares.append(since / every)
            if rate > 0:
                days_left_by.append(max(0.0, (every - since) / rate))
    if task.every_days:
        shares.append(days_since / task.every_days)
        days_left_by.append(max(0, task.every_days - days_since))
    flagged = task.flagged_at is not None
    share = 1.0 if flagged else (max(shares) if shares else 0)
    status = "due" if share >= 1 else "soon" if share >= SOON else "ok"
    predicted = (now + timedelta(days=min(days_left_by))).date().isoformat() if days_left_by and status != "due" else None
    return {"id": task.id, "printer_id": task.printer_id, "printer": names.get(task.printer_id), "name": task.name,
            "every_hours": task.every_hours, "every_days": task.every_days, "every_grams": task.every_grams, "every_prints": task.every_prints,
            "every_failures": task.every_failures, "failure_reason": task.failure_reason, "note": task.note,
            "flagged": flagged, "flagged_note": task.flagged_note, "grams_since": used["grams"], "prints_since": used["prints"], "failures_since": used["failures"],
            "predicted_due": predicted,
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
    for row in session.exec(select(MaintenanceLog).where(MaintenanceLog.printer_id == printer_id)).all():
        session.delete(row)                                    # SQLite reuses ids: a history must never pass to a later printer


HMS_CODE = re.compile(r"^HMS_[0-9A-F]{4}_[0-9A-F]{4}_[0-9A-F]{4}_[0-9A-F]{4}$")


def hms_map(session: Session) -> dict:
    try:
        data = json.loads(get_setting(session, "hms_tasks", "{}") or "{}")
    except ValueError:
        return {}
    return {str(k).upper(): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def apply_hms(session: Session) -> list:
    """A Bambu printer reports a fault code (HMS) that you have tied to a maintenance task: that task is marked as having a problem and is due now.
    Returns the names of tasks newly flagged."""
    from app import printwatch
    mapping = hms_map(session)
    if not mapping:
        return []
    flagged = []
    for printer_id, status in list(printwatch.latest.items()):
        for code in status.get("hms") or []:
            name = mapping.get(str(code).upper())
            if not name:
                continue
            task = next((t for t in session.exec(select(MaintenanceTask).where(MaintenanceTask.printer_id == printer_id)).all() if t.name.lower() == name.lower()), None)
            if task and task.flagged_at is None:
                task.flagged_at, task.flagged_note = datetime.utcnow(), f"Bambu reported {code}"
                session.add(task)
                flagged.append(f"{status.get('name') or 'A printer'}: {task.name}")
                try:
                    before = set(json.loads(get_setting(session, "maintenance_notified", "[]")))
                except ValueError:
                    before = set()
                session.flush()
                set_setting(session, "maintenance_notified", json.dumps(sorted(before | {task.id})))
    if flagged:
        session.commit()
        notify_event(session, "maintenance_due", "Model Hub: printer maintenance due", "; ".join(flagged[:6]))
    return flagged


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
