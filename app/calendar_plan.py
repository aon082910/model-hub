"""A calendar of planned prints.

A print-queue entry can have a planned day. This builds the month view (what is planned, what was printed, and whether a day is
more than your printers can do), plans waiting entries into free days by itself when asked, and writes an .ics file that a
phone or desktop calendar can read.
"""
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models import Model3D, Printer, PrintLog, QueueItem
from app.settings_store import get_setting

DEFAULT_HOURS = 12.0
GUESS_MINUTES = 60              # an entry with no estimate takes this much room in a day
HORIZON_DAYS = 365
OPEN = ("queued", "printing")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def hours_per_day(session: Session) -> float:
    try:
        hours = float(get_setting(session, "calendar_hours_per_day", "") or DEFAULT_HOURS)
    except ValueError:
        hours = DEFAULT_HOURS
    return min(24.0, max(1.0, hours))


def capacity_minutes(session: Session) -> int:
    """How many minutes of printing one day can hold: the hours you let printers run, times the printers you have."""
    printers = max(1, len(session.exec(select(Printer.id)).all()))
    return int(hours_per_day(session) * 60 * printers)


def _names(session: Session, ids) -> dict:
    ids = list(set(ids))
    if not ids:
        return {}
    return {m.id: m.filename for m in session.exec(select(Model3D).where(Model3D.id.in_(ids))).all()}


def month_view(session: Session, month: str) -> dict:
    if not MONTH_RE.match(month or ""):
        raise ValueError("The month must look like 2026-10")
    year, number = int(month[:4]), int(month[5:])
    first, last = date(year, number, 1), date(year, number, monthrange(year, number)[1])
    capacity = capacity_minutes(session)
    planned = session.exec(select(QueueItem).where(QueueItem.planned_date >= first.isoformat(), QueueItem.planned_date <= last.isoformat(),
                                                   QueueItem.status.in_(OPEN)).order_by(QueueItem.position)).all()
    logs = session.exec(select(PrintLog).where(PrintLog.printed_at >= datetime.combine(first, datetime.min.time()),
                                               PrintLog.printed_at < datetime.combine(last + timedelta(days=1), datetime.min.time()))
                        .order_by(PrintLog.printed_at)).all()
    names = _names(session, [i.model_id for i in planned] + [l.model_id for l in logs])
    printers = {p.id: p.name for p in session.exec(select(Printer)).all()}
    from app.routers.prints import photo_path
    days: dict = {}
    for i in planned:
        day = days.setdefault(i.planned_date, {"planned": [], "printed": [], "minutes": 0, "overbooked": False})
        minutes = i.estimated_minutes or 0
        day["planned"].append({"id": i.id, "model_id": i.model_id, "filename": names.get(i.model_id), "status": i.status, "minutes": minutes or None,
                               "printer": printers.get(i.printer_id)})
        day["minutes"] += minutes or GUESS_MINUTES
        day["overbooked"] = day["minutes"] > capacity
    for log in logs:
        day = days.setdefault(log.printed_at.date().isoformat(), {"planned": [], "printed": [], "minutes": 0, "overbooked": False})
        day["printed"].append({"id": log.id, "model_id": log.model_id, "filename": names.get(log.model_id), "minutes": log.minutes,
                               "has_photo": photo_path(log.id).is_file()})
    waiting = session.exec(select(QueueItem).where(QueueItem.status == "queued", QueueItem.planned_date.is_(None))
                           .order_by(QueueItem.position)).all()
    unplanned = [{"id": i.id, "model_id": i.model_id, "minutes": i.estimated_minutes or None} for i in waiting[:100]]
    names.update(_names(session, [u["model_id"] for u in unplanned]))
    for u in unplanned:
        u["filename"] = names.get(u["model_id"])
    return {"month": month, "first": first.isoformat(), "last": last.isoformat(), "days": days, "unplanned": unplanned,
            "unplanned_total": len(waiting), "capacity_minutes": capacity, "hours_per_day": hours_per_day(session)}


def auto_plan(session: Session, start: Optional[date] = None, dry_run: bool = False) -> dict:
    """Give every waiting entry without a day the first day (from `start`) with room for it, in queue order.
    Returns {"assigned": [{id, planned_date, minutes, guessed}], "unplaced": n}; commits unless dry_run."""
    start = start or date.today()
    capacity = capacity_minutes(session)
    used: dict = {}
    for i in session.exec(select(QueueItem).where(QueueItem.planned_date >= start.isoformat(), QueueItem.status.in_(OPEN))).all():
        used[i.planned_date] = used.get(i.planned_date, 0) + (i.estimated_minutes or GUESS_MINUTES)
    waiting = session.exec(select(QueueItem).where(QueueItem.status == "queued", QueueItem.planned_date.is_(None))
                           .order_by(QueueItem.position)).all()
    assigned, unplaced = [], 0
    for item in waiting:
        need = item.estimated_minutes or GUESS_MINUTES
        for offset in range(HORIZON_DAYS):
            day = (start + timedelta(days=offset)).isoformat()
            # a job longer than a whole day still gets a day, but only an empty one
            if used.get(day, 0) + need <= capacity or used.get(day, 0) == 0:
                used[day] = used.get(day, 0) + need
                assigned.append({"id": item.id, "planned_date": day, "minutes": need, "guessed": not item.estimated_minutes})
                if not dry_run:
                    item.planned_date = day
                    session.add(item)
                break
        else:
            unplaced += 1
    if not dry_run:
        session.commit()
    return {"assigned": assigned, "unplaced": unplaced}


def _ics_text(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value or "")
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Lines are at most 75 octets; longer ones continue on the next line after a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > (75 if not parts else 74):
            parts.append(current)
            current = b""
        current += encoded
    parts.append(current)
    return "\r\n ".join(p.decode("utf-8") for p in parts)


def ics(session: Session) -> str:
    """Planned (not yet finished) prints as all-day events."""
    items = session.exec(select(QueueItem).where(QueueItem.planned_date.is_not(None), QueueItem.status.in_(OPEN))
                         .order_by(QueueItem.planned_date, QueueItem.position)).all()
    names = _names(session, [i.model_id for i in items])
    printers = {p.id: p.name for p in session.exec(select(Printer)).all()}
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Model Hub//Planned prints//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Model Hub prints"]
    for i in items:
        try:
            day = date.fromisoformat(i.planned_date)
        except (TypeError, ValueError):
            continue
        detail = []
        if i.estimated_minutes:
            detail.append(f"about {round(i.estimated_minutes)} min")
        if printers.get(i.printer_id):
            detail.append(f"on {printers[i.printer_id]}")
        lines += ["BEGIN:VEVENT", f"UID:queue-{i.id}@modelhub", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
                  f"SUMMARY:{_ics_text('Print: ' + (names.get(i.model_id) or 'a model'))}"]
        if detail:
            lines.append(f"DESCRIPTION:{_ics_text(', '.join(detail))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
