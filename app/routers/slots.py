from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from app import activity, slots
from app.db import get_session
from app.models import Printer

router = APIRouter(prefix="/api/slots", tags=["slots"])


@router.get("")
def list_slots(session: Session = Depends(get_session)):
    """The printers that have spool slots and what is loaded in each."""
    return {"printers": slots.overview(session)}


@router.post("/{printer_id}/apply-suggestions")
def apply_suggestions(printer_id: int, request: Request, session: Session = Depends(get_session)):
    """Load the spools the printer's own report points at into the empty slots (never replacing one you chose)."""
    printer = session.get(Printer, printer_id)
    if not printer:
        raise HTTPException(404, "That printer was not found")
    entry = next((p for p in slots.overview(session) if p["id"] == printer_id), None)
    done = []
    for slot in (entry["slots"] if entry else []):
        if slot["suggested_filament_id"]:
            slots.load(session, printer, slot["slot"], slot["suggested_filament_id"])
            done.append(slot["slot"])
            entry = next((p for p in slots.overview(session) if p["id"] == printer_id), entry)      # a spool can only be suggested once
    if done:
        activity.record(session, activity.actor_of(request), "slot", f"{printer.name}: loaded spools into slots {', '.join(map(str, done))} from the printer's report")
    return {"loaded": done}


@router.post("/{printer_id}/sync-remaining")
def sync_remaining(printer_id: int, request: Request, session: Session = Depends(get_session)):
    """Set each loaded spool's remaining weight from the percentage the printer reports for its slot (Bambu spools with a tag
    report this; others report nothing, and those are left alone)."""
    from app.models import Filament
    printer = session.get(Printer, printer_id)
    if not printer:
        raise HTTPException(404, "That printer was not found")
    entry = next((p for p in slots.overview(session) if p["id"] == printer_id), None)
    changed = []
    for slot in (entry["slots"] if entry else []):
        tray, spool = slot["reported"], slot["spool"]
        if spool and tray and tray.get("remain") is not None:
            row = session.get(Filament, spool["id"])
            weight = round((row.spool_weight_g or 1000) * tray["remain"] / 100, 1)
            if row and abs(weight - row.remaining_g) >= 1:
                changed.append({"slot": slot["slot"], "spool_id": row.id, "from": row.remaining_g, "to": weight})
                row.remaining_g = weight
                session.add(row)
    session.commit()
    if changed:
        activity.record(session, activity.actor_of(request), "slot", f"{printer.name}: remaining weight of {len(changed)} spool(s) set from the printer's report")
    return {"changed": changed}


@router.put("/{printer_id}/{slot}")
def load_slot(printer_id: int, slot: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    """Put a spool in a slot (filament_id), or empty it (null). label names the slot ("AMS A1", "tool 2")."""
    printer = session.get(Printer, printer_id)
    if not printer:
        raise HTTPException(404, "That printer was not found")
    filament_id = payload.get("filament_id")
    if filament_id is not None and (not isinstance(filament_id, int) or isinstance(filament_id, bool)):
        raise HTTPException(400, "filament_id must be a number or null")
    label = payload.get("label")
    if label is not None and not isinstance(label, str):
        raise HTTPException(400, "label must be text")
    try:
        row = slots.load(session, printer, slot, filament_id, label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    activity.record(session, activity.actor_of(request), "slot",
                    f"{printer.name} slot {slot}: " + ("emptied" if filament_id is None else f"spool {filament_id} loaded"))
    return next(s for p in slots.overview(session) if p["id"] == printer_id for s in p["slots"] if s["slot"] == row.slot)
