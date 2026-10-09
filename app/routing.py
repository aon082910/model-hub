"""Which printer should a waiting print go to? Looks at the bed, the spool loaded, and how much each printer already has to do.
It only suggests; assigning is a button, and can be undone."""
from typing import Optional

from sqlmodel import Session, select

from app import fit, sensors, slots
from app.models import Filament, Model3D, Printer, QueueItem


def strict_wanted(session: Session, item: QueueItem) -> bool:
    if item.strict_match is not None:
        return bool(item.strict_match)
    from app.settings_store import get_setting
    return get_setting(session, "strict_colour", "") == "true"


def same_spool_kind(a, b) -> bool:
    """The same material and the same colour (by name or by hex)."""
    if str(a.material or "").strip().lower() != str(b.material or "").strip().lower():
        return False
    ca, cb = str(a.color or "").strip().lower(), str(b.color or "").strip().lower()
    ha, hb = str(a.color_hex or "").strip().lower(), str(b.color_hex or "").strip().lower()
    return bool((ca and ca == cb) or (ha and ha == hb))


def exact_match_in(spool, entry, session: Session) -> bool:
    """Does the printer (its overview entry) have a spool of exactly this material and colour loaded?"""
    if not entry:
        return False
    for slot in entry["slots"]:
        loaded = session.get(Filament, slot["filament_id"]) if slot["filament_id"] else None
        if loaded and (loaded.id == spool.id or same_spool_kind(loaded, spool)):
            return True
    return False


def printers_with_files(session: Session, model_id: int):
    """Printer ids a model's sliced files are made for, or None when it has a file that suits any printer (or none at all)."""
    from app import print_files
    from app.models import PrintFile
    files = [f for f in session.exec(select(PrintFile).where(PrintFile.model_id == model_id)).all() if f.kind in print_files.GCODE_KINDS]
    if not files or any(f.printer_id is None for f in files):
        return None
    return {f.printer_id for f in files}


def tag_list(printer: Printer) -> list:
    return [t for t in (printer.tags or "").split(",") if t]


def _load_minutes(session: Session, printer_id: int) -> float:
    return sum(float(q.estimated_minutes or 60) for q in session.exec(select(QueueItem).where(QueueItem.printer_id == printer_id, QueueItem.status.in_(["queued", "printing"]))).all())


def suggest_for(session: Session, item: QueueItem, printers: list, overview: dict) -> list:
    model = session.get(Model3D, item.model_id)
    spool = session.get(Filament, item.filament_id) if item.filament_id else None
    out = []
    only = printers_with_files(session, item.model_id)
    strict = strict_wanted(session, item) and spool is not None
    for printer in printers:
        reasons, score, slot = [], 100.0, None
        if sensors.hold_reason(session, printer.id):
            continue                                                      # a sensor says it should not be started now
        if only is not None and printer.id not in only:
            continue                                                      # its sliced files are all made for other printers
        if strict and not exact_match_in(spool, overview.get(printer.id), session):
            continue                                                      # exact colour wanted and not loaded here
        if item.printer_tag and item.printer_tag.lower() not in tag_list(printer):
            continue                                                      # it asked for a tag this printer does not have
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
                if item.estimated_grams and spool.remaining_g < item.estimated_grams:
                    score -= 60
                    reasons.append(f"but only {spool.remaining_g:g} g of it is left, and this print needs about {item.estimated_grams:g} g")
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
    stmt = select(QueueItem).where(QueueItem.status == "queued", QueueItem.printer_id.is_(None), QueueItem.held.is_not(True)).order_by(QueueItem.position)
    result = {}
    for item in session.exec(stmt).all():
        if item_ids is not None and item.id not in item_ids:
            continue
        found = suggest_for(session, item, printers, overview)
        if found:
            result[item.id] = found[:3]
    return result
