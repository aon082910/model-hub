"""A calendar of planned prints.

A print-queue entry can have a planned day. This builds the month view (what is planned, what was printed, whether a day is
more than your printers can do, and whether the filament you have will last through the plan), plans waiting entries into free
days by itself when asked, and writes an .ics file that a phone or desktop calendar can read.

Each printer has its own day: it can run hours_per_day. An entry with no printer chosen can go on any of them, so the day as a
whole can hold hours_per_day times the number of printers (at least one).
"""
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models import Filament, Model3D, Printer, PrintLog, QueueItem
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


def printer_names(session: Session) -> dict:
    return {p.id: p.name for p in session.exec(select(Printer).order_by(Printer.name)).all()}


def capacity_minutes(session: Session) -> int:
    """How many minutes of printing one day can hold in all: the hours you let printers run, times the printers you have."""
    return int(hours_per_day(session) * 60 * max(1, len(printer_names(session))))


def parse_printer(value) -> Optional[object]:
    """The printer a view is limited to: None for every printer, "none" for entries with no printer, else a printer id.
    Raises ValueError for anything else."""
    if value in (None, "", "all"):
        return None
    if value == "none":
        return "none"
    try:
        number = int(str(value))
    except ValueError:
        raise ValueError("printer must be a printer id, none or empty")
    if number <= 0:
        raise ValueError("printer must be a printer id, none or empty")
    return number


def _owner(item: QueueItem, names: dict) -> Optional[int]:
    """The printer an entry counts against: its own, or None when it has none (or that printer was removed)."""
    return item.printer_id if item.printer_id in names else None


def _names(session: Session, ids) -> dict:
    ids = list(set(ids))
    if not ids:
        return {}
    return {m.id: m.filename for m in session.exec(select(Model3D).where(Model3D.id.in_(ids))).all()}


def filament_shortfalls(session: Session) -> dict:
    """{queue entry id: {"spool_id", "spool", "need", "short_by"}} for planned entries whose spool will not have enough left,
    taking the planned entries in date order and counting what the earlier ones will use up."""
    items = session.exec(select(QueueItem).where(QueueItem.planned_date.is_not(None), QueueItem.status.in_(OPEN),
                                                 QueueItem.estimated_grams > 0)
                         .order_by(QueueItem.planned_date, QueueItem.position, QueueItem.id)).all()
    from app import slots
    chosen = {i.id: slots.effective_filament(session, i) for i in items}          # the spool in the entry's slot, else the one chosen
    items = [i for i in items if chosen[i.id]]
    spools = {f.id: f for f in session.exec(select(Filament).where(Filament.id.in_(set(chosen.values()) - {None} or {0}))).all()}
    left = {sid: float(f.remaining_g or 0) for sid, f in spools.items()}
    short = {}
    for item in items:
        spool = spools.get(chosen[item.id])
        if not spool:
            continue
        have = max(0.0, left[spool.id])
        need = float(item.estimated_grams)
        if need > have:
            label = " ".join(x for x in (spool.material, spool.brand, spool.color) if x) or "a spool"
            short[item.id] = {"spool_id": spool.id, "spool": label, "need": round(need, 1), "short_by": round(need - have, 1)}
        left[spool.id] -= need
    return short


def month_view(session: Session, month: str, printer=None) -> dict:
    """One month. printer: None for every printer, "none" for entries without one, or a printer id (see parse_printer)."""
    if not MONTH_RE.match(month or ""):
        raise ValueError("The month must look like 2026-10")
    year, number = int(month[:4]), int(month[5:])
    first, last = date(year, number, 1), date(year, number, monthrange(year, number)[1])
    names_of_printers = printer_names(session)
    if isinstance(printer, int) and printer not in names_of_printers:
        raise ValueError("That printer does not exist")
    per_printer_cap = int(hours_per_day(session) * 60)
    capacity = per_printer_cap if isinstance(printer, int) else capacity_minutes(session)

    def in_view(item: QueueItem) -> bool:
        owner = _owner(item, names_of_printers)
        return printer is None or (printer == "none" and owner is None) or (isinstance(printer, int) and owner == printer)

    planned = [i for i in session.exec(select(QueueItem).where(QueueItem.planned_date >= first.isoformat(), QueueItem.planned_date <= last.isoformat(),
                                                               QueueItem.status.in_(OPEN)).order_by(QueueItem.position)).all() if in_view(i)]
    # what was printed is not tied to a printer, so it only shows when every printer is shown
    logs = [] if printer is not None else session.exec(
        select(PrintLog).where(PrintLog.printed_at >= datetime.combine(first, datetime.min.time()),
                               PrintLog.printed_at < datetime.combine(last + timedelta(days=1), datetime.min.time()))
        .order_by(PrintLog.printed_at)).all()
    names = _names(session, [i.model_id for i in planned] + [l.model_id for l in logs])
    short = filament_shortfalls(session)
    from app.routers.prints import photo_path
    days: dict = {}
    by_printer: dict = {}                      # (day, printer id or None) -> minutes
    for i in planned:
        day = days.setdefault(i.planned_date, {"planned": [], "printed": [], "minutes": 0, "overbooked": False, "short": False})
        minutes = i.estimated_minutes or 0
        entry = {"id": i.id, "model_id": i.model_id, "filename": names.get(i.model_id), "status": i.status, "minutes": minutes or None,
                 "printer": names_of_printers.get(i.printer_id), "printer_id": _owner(i, names_of_printers), "short": short.get(i.id)}
        day["planned"].append(entry)
        day["minutes"] += minutes or GUESS_MINUTES
        key = (i.planned_date, _owner(i, names_of_printers))
        by_printer[key] = by_printer.get(key, 0) + (minutes or GUESS_MINUTES)
        day["short"] = day["short"] or bool(short.get(i.id))
    for (date_key, owner), minutes in by_printer.items():
        day = days[date_key]
        # one printer cannot do more than its own day, and the whole day cannot hold more than all printers together
        day["overbooked"] = day["overbooked"] or day["minutes"] > capacity or (owner is not None and minutes > per_printer_cap)
    for log in logs:
        day = days.setdefault(log.printed_at.date().isoformat(), {"planned": [], "printed": [], "minutes": 0, "overbooked": False, "short": False})
        day["printed"].append({"id": log.id, "model_id": log.model_id, "filename": names.get(log.model_id), "minutes": log.minutes,
                               "has_photo": photo_path(log.id).is_file(), "failed": log.outcome == "failed", "reason": log.failure_reason})
    waiting = [i for i in session.exec(select(QueueItem).where(QueueItem.status == "queued", QueueItem.planned_date.is_(None))
                                       .order_by(QueueItem.position)).all() if in_view(i)]
    unplanned = [{"id": i.id, "model_id": i.model_id, "minutes": i.estimated_minutes or None, "printer": names_of_printers.get(i.printer_id)}
                 for i in waiting[:100]]
    names.update(_names(session, [u["model_id"] for u in unplanned]))
    for u in unplanned:
        u["filename"] = names.get(u["model_id"])
    return {"month": month, "first": first.isoformat(), "last": last.isoformat(), "days": days, "unplanned": unplanned,
            "unplanned_total": len(waiting), "capacity_minutes": capacity, "hours_per_day": hours_per_day(session),
            "printer": printer, "printers": [{"id": k, "name": v} for k, v in names_of_printers.items()],
            "short_count": sum(1 for d in days.values() for p in d["planned"] if p["short"])}


def auto_plan(session: Session, start: Optional[date] = None, dry_run: bool = False) -> dict:
    """Give every waiting entry without a day the first day (from `start`) with room for it, in queue order. An entry meant for
    a printer needs room on that printer; one with no printer needs room somewhere.
    Returns {"assigned": [{id, planned_date, minutes, guessed}], "unplaced": n}; commits unless dry_run."""
    start = start or date.today()
    names_of_printers = printer_names(session)
    per_printer_cap = int(hours_per_day(session) * 60)
    capacity = capacity_minutes(session)
    used_total: dict = {}
    used_printer: dict = {}
    for i in session.exec(select(QueueItem).where(QueueItem.planned_date >= start.isoformat(), QueueItem.status.in_(OPEN))).all():
        minutes = i.estimated_minutes or GUESS_MINUTES
        used_total[i.planned_date] = used_total.get(i.planned_date, 0) + minutes
        owner = _owner(i, names_of_printers)
        if owner is not None:
            used_printer[(i.planned_date, owner)] = used_printer.get((i.planned_date, owner), 0) + minutes
    waiting = session.exec(select(QueueItem).where(QueueItem.status == "queued", QueueItem.planned_date.is_(None))
                           .order_by(QueueItem.position)).all()
    assigned, unplaced = [], 0
    for item in waiting:
        need = item.estimated_minutes or GUESS_MINUTES
        owner = _owner(item, names_of_printers)
        for offset in range(HORIZON_DAYS):
            day = (start + timedelta(days=offset)).isoformat()
            if owner is None:
                # a job longer than a whole day still gets a day, but only an empty one
                fits = used_total.get(day, 0) + need <= capacity or used_total.get(day, 0) == 0
            else:
                mine = used_printer.get((day, owner), 0)
                fits = mine + need <= per_printer_cap or mine == 0
            if fits:
                used_total[day] = used_total.get(day, 0) + need
                if owner is not None:
                    used_printer[(day, owner)] = used_printer.get((day, owner), 0) + need
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


COPY_STATUSES = ("queued", "printing", "done", "failed")
MAX_COPIED = 200


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def copy_weeks(session: Session, source_day: date, weeks: int = 1, statuses=("queued", "printing", "done"), dry_run: bool = False) -> dict:
    """Repeat the plan of the week (Monday to Sunday) that contains source_day for the next `weeks` weeks: every queue entry
    planned in that week (with one of `statuses`) is copied, as a waiting entry, to the same weekday a week later (and two, three...).
    Returns {"week": first day, "created": [{id, source_id, planned_date}]}. Nothing is copied twice by mistake: it is a button you press."""
    if not 1 <= weeks <= 8:
        raise ValueError("Repeat for 1 to 8 weeks")
    wanted = [s for s in statuses if s in COPY_STATUSES]
    if not wanted:
        raise ValueError("Choose which entries to copy")
    start = week_start(source_day)
    items = session.exec(select(QueueItem).where(QueueItem.planned_date >= start.isoformat(), QueueItem.planned_date < (start + timedelta(days=7)).isoformat(),
                                                 QueueItem.status.in_(wanted)).order_by(QueueItem.planned_date, QueueItem.position, QueueItem.id)).all()
    if len(items) * weeks > MAX_COPIED:
        raise ValueError(f"That would add more than {MAX_COPIED} entries")
    top = session.exec(select(QueueItem).order_by(QueueItem.position.desc())).first()
    position = (top.position + 1) if top else 0
    created = []
    for week in range(1, weeks + 1):
        for item in items:
            day = (date.fromisoformat(item.planned_date) + timedelta(days=7 * week)).isoformat()
            entry = {"source_id": item.id, "planned_date": day, "model_id": item.model_id}
            if not dry_run:
                copy = QueueItem(model_id=item.model_id, position=position, status="queued", printer_id=item.printer_id, slot=item.slot,
                                 filament_id=item.filament_id, notes=item.notes, estimated_grams=item.estimated_grams,
                                 estimated_minutes=item.estimated_minutes, estimate_basis=item.estimate_basis, planned_date=day)
                session.add(copy)
                session.flush()
                entry["id"] = copy.id
                position += 1
            created.append(entry)
    if not dry_run:
        session.commit()
    return {"week": start.isoformat(), "created": created}


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


def ics(session: Session, printer=None) -> str:
    """Planned (not yet finished) prints as all-day events; printer limits it like month_view does."""
    names_of_printers = printer_names(session)
    items = [i for i in session.exec(select(QueueItem).where(QueueItem.planned_date.is_not(None), QueueItem.status.in_(OPEN))
                                     .order_by(QueueItem.planned_date, QueueItem.position)).all()
             if printer is None or (printer == "none" and _owner(i, names_of_printers) is None)
             or (isinstance(printer, int) and _owner(i, names_of_printers) == printer)]
    names = _names(session, [i.model_id for i in items])
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    title = "Model Hub prints" + (f" ({names_of_printers.get(printer)})" if isinstance(printer, int) and printer in names_of_printers else "")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Model Hub//Planned prints//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{_ics_text(title)}"]
    for i in items:
        try:
            day = date.fromisoformat(i.planned_date)
        except (TypeError, ValueError):
            continue
        detail = []
        if i.estimated_minutes:
            detail.append(f"about {round(i.estimated_minutes)} min")
        if names_of_printers.get(i.printer_id):
            detail.append(f"on {names_of_printers[i.printer_id]}")
        lines += ["BEGIN:VEVENT", f"UID:queue-{i.id}@modelhub", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
                  f"SUMMARY:{_ics_text('Print: ' + (names.get(i.model_id) or 'a model'))}"]
        if detail:
            lines.append(f"DESCRIPTION:{_ics_text(', '.join(detail))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
