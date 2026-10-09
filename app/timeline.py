"""When each waiting print would run: the printers' queues laid out in time, and when a print would finish if it were started right now.

It is arithmetic over the estimates (so only as good as they are): a print that is running takes what is left of its estimate (from the printer's progress), the
waiting prints assigned to a printer follow one after another in queue order, a held print waits for a release and so takes no time in the plan, and a print
without an estimate cannot be placed. Nothing is started or changed by looking at this."""
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models import Model3D, Printer, QueueItem


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() + "Z" if moment else None


def build(session: Session, now: Optional[datetime] = None) -> dict:
    from app import printwatch
    now = now or datetime.utcnow()
    printers = session.exec(select(Printer).order_by(Printer.name)).all()
    items = session.exec(select(QueueItem).where(QueueItem.status.in_(["queued", "printing"])).order_by(QueueItem.position, QueueItem.id)).all()
    names = {m.id: m.filename for m in session.exec(select(Model3D).where(Model3D.id.in_({i.model_id for i in items} or {0}))).all()}
    idle = {p.id for p in printers if (printwatch.latest.get(p.id) or {}).get("online") and (printwatch.latest.get(p.id) or {}).get("state") not in ("printing", "paused")}
    any_idle = bool(idle)
    rows, if_now = [], {}

    def entry(item, start, end):
        return {"id": item.id, "model_id": item.model_id, "model": names.get(item.model_id), "status": item.status, "held": bool(item.held), "minutes": item.estimated_minutes,
                "start": _iso(start), "end": _iso(end)}

    for printer in printers:
        cursor = now
        mine = [i for i in items if i.printer_id == printer.id]
        out = []
        for item in (i for i in mine if i.status == "printing"):
            progress = (printwatch.latest.get(printer.id) or {}).get("progress")
            minutes = float(item.estimated_minutes) if item.estimated_minutes else None
            left = minutes * (1 - min(max(progress or 0, 0), 100) / 100) if minutes is not None else None
            end = now + timedelta(minutes=left) if left is not None else None
            out.append(entry(item, now, end))
            if end:
                cursor = max(cursor, end)
        for item in (i for i in mine if i.status == "queued"):
            if item.held or not item.estimated_minutes:
                out.append(entry(item, None, None))
                continue
            end = cursor + timedelta(minutes=float(item.estimated_minutes))
            out.append(entry(item, cursor, end))
            if printer.id in idle and not any(i["status"] == "printing" for i in out) and item.id not in if_now:
                if_now[item.id] = _iso(now + timedelta(minutes=float(item.estimated_minutes)))
            cursor = end
        rows.append({"id": printer.id, "name": printer.name, "free_at": _iso(cursor), "items": out})
    loose = []
    for item in (i for i in items if i.printer_id is None and i.status == "queued"):
        loose.append(entry(item, None, None))
        if any_idle and not item.held and item.estimated_minutes:
            if_now[item.id] = _iso(now + timedelta(minutes=float(item.estimated_minutes)))
    return {"now": _iso(now), "printers": rows, "unassigned": loose, "if_started_now": {str(k): v for k, v in if_now.items()}}
