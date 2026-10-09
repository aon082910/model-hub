"""Which printer should a waiting print go to? Looks at the bed, the spool loaded, and how much each printer already has to do.
It only suggests; assigning is a button, and can be undone."""
from typing import Optional

from sqlmodel import Session, select

from app import fit, slots
from app.models import Filament, Model3D, Printer, QueueItem


def _load_minutes(session: Session, printer_id: int) -> float:
    return sum(float(q.estimated_minutes or 60) for q in session.exec(select(QueueItem).where(QueueItem.printer_id == printer_id, QueueItem.status.in_(["queued", "printing"]))).all())


def suggest_for(session: Session, item: QueueItem, printers: list, overview: dict) -> list:
    model = session.get(Model3D, item.model_id)
    spool = session.get(Filament, item.filament_id) if item.filament_id else None
    out = []
    for printer in printers:
        reasons, score, slot = [], 100.0, None
        verdict = fit.fits(model, printer) if model else None
        if verdict is False:
            continue                                                      # it does not fit: not an option
        if verdict:
            reasons.append("fits the bed")
        minutes = _load_minutes(session, printer.id)
        score -= min(minutes / 60, 40)
        reasons.append("nothing waiting" if minutes == 0 else f"about {minutes / 60:.1f} h already waiting")
        entry = overview.get(printer.id)
        if spool and entry:
            exact = next((s for s in entry["slots"] if s["filament_id"] == spool.id), None)
            same = next((s for s in entry["slots"] if s["spool"] and str(s["spool"]["material"] or "").lower() == str(spool.material or "").lower()), None)
            if exact:
                score += 30
                slot = exact["slot"]
                reasons.append(f"has that spool in slot {slot}")
            elif same:
                score += 12
                slot = same["slot"]
                reasons.append(f"has {spool.material} in slot {slot}")
            else:
                score -= 15
                reasons.append(f"no {spool.material} loaded")
        out.append({"printer_id": printer.id, "name": printer.name, "slot": slot, "score": round(score, 1), "reasons": reasons})
    return sorted(out, key=lambda r: (-r["score"], r["name"]))


def suggestions(session: Session, item_ids: Optional[list] = None) -> dict:
    """{queue entry id: [candidates, best first]} for waiting entries that have no printer yet."""
    printers = session.exec(select(Printer).order_by(Printer.name)).all()
    overview = {p["id"]: p for p in slots.overview(session)}
    stmt = select(QueueItem).where(QueueItem.status == "queued", QueueItem.printer_id.is_(None)).order_by(QueueItem.position)
    result = {}
    for item in session.exec(stmt).all():
        if item_ids is not None and item.id not in item_ids:
            continue
        found = suggest_for(session, item, printers, overview)
        if found:
            result[item.id] = found[:3]
    return result
