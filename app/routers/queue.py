import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select
from app.db import get_session
from app.models import Model3D, Printer, PrinterJob, PrintFile, QueueItem, Filament, PrintLog

router = APIRouter(prefix="/api/queue", tags=["queue"])

def check_uses(session: Session, printer_id, value) -> Optional[str]:
    """A multicolour job's spools as JSON text, or None. Each use names a slot of the entry's printer or a spool, and grams."""
    import json
    from app import slots
    if value in (None, "", []):
        return None
    if not isinstance(value, list) or len(value) > 16:
        raise HTTPException(400, "uses must be a list of at most 16 spools")
    clean = []
    for row in value:
        if not isinstance(row, dict):
            raise HTTPException(400, "Each spool in uses needs a slot or a spool, and grams")
        grams = row.get("grams")
        if isinstance(grams, bool) or not isinstance(grams, (int, float)) or not 0 < grams <= 100000:
            raise HTTPException(400, "grams must be a number above 0")
        slot, spool = row.get("slot"), row.get("filament_id")
        if slot is None and spool is None:
            raise HTTPException(400, "Each spool in uses needs a slot or a spool")
        entry = {"grams": round(float(grams), 2)}
        if slot is not None:
            entry["slot"] = check_slot(session, printer_id, slot)
        if spool is not None:
            if isinstance(spool, bool) or not isinstance(spool, int) or not session.get(Filament, spool):
                raise HTTPException(400, "That filament spool does not exist")
            entry["filament_id"] = spool
        clean.append(entry)
    return json.dumps(clean)


def check_slot(session: Session, printer_id, slot):
    """A slot number for a queue entry: needs a printer that has that slot. None clears it."""
    from app import slots
    if slot in (None, ""):
        return None
    if not isinstance(slot, int) or isinstance(slot, bool):
        raise HTTPException(400, "slot must be a number")
    printer = session.get(Printer, printer_id) if printer_id else None
    if not printer:
        raise HTTPException(400, "Choose the printer before the slot")
    if not 1 <= slot <= slots.slot_count(printer):
        raise HTTPException(400, f"{printer.name} has no slot {slot}")
    return slot


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clean_date(value) -> Optional[str]:
    """A calendar day as YYYY-MM-DD, or None to clear it."""
    if value in (None, ""):
        return None
    from datetime import date
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise HTTPException(400, "The date must look like 2026-10-31")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "That is not a real date")
    return value


@router.get("")
def list_queue(session: Session = Depends(get_session)):
    items = session.exec(select(QueueItem).order_by(QueueItem.position)).all()
    return items


@router.post("")
def add_to_queue(payload: dict, session: Session = Depends(get_session)):
    max_pos = session.exec(select(QueueItem).order_by(QueueItem.position.desc())).first()
    position = (max_pos.position + 1) if max_pos else 0
    printer_id = payload.get("printer_id")
    if printer_id is not None and not session.get(Printer, printer_id):
        raise HTTPException(400, "That printer does not exist")
    slot = check_slot(session, printer_id, payload.get("slot"))
    filament_id = payload.get("filament_id")
    if slot and not filament_id:
        from app import slots
        filament_id = slots.loaded(session, printer_id, slot)               # the spool in that slot
    uses = check_uses(session, printer_id, payload.get("uses"))
    total = round(sum(u["grams"] for u in json.loads(uses)), 1) if uses else None
    minutes, basis = payload.get("estimated_minutes"), "manual"
    if not minutes:
        from app import learned
        suggestion = learned.suggest(session, payload["model_id"])
        minutes, basis = suggestion["minutes"], suggestion["basis"]
    item = QueueItem(
        printer_id=printer_id, slot=slot,
        model_id=payload["model_id"],
        filament_id=filament_id,
        notes=payload.get("notes"),
        estimated_grams=payload.get("estimated_grams") or total, uses=uses,
        estimated_minutes=minutes,
        estimate_basis=basis if minutes else None,
        planned_date=clean_date(payload.get("planned_date")),
        position=position,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def partial_parts(session: Session, item: QueueItem, progress) -> list:
    """What a failed print probably used: the share of each spool's grams that matches how far it got, or nothing when the
    progress is not known, the switch in Settings is off, or it is too little to matter."""
    from app import slots
    from app.settings_store import get_setting
    if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 < progress < 100:
        return []
    if get_setting(session, "failed_deduct", "") == "false":
        return []
    parts = [(spool_id, round(grams * progress / 100, 1)) for spool_id, grams in slots.spool_parts(session, item)]
    return [p for p in parts if p[1] >= 0.5]


def fail_item(session: Session, item: QueueItem, reason: Optional[str] = None, minutes: Optional[float] = None,
              printer_id: Optional[int] = None, progress=None) -> None:
    """Mark a queue entry failed and keep a failed entry in the print log (once per entry). When the printer said how far it
    got, the filament that went into the failed part comes off the spool (a share of the job's grams); otherwise nothing is
    taken, because how much was used is not known: edit the log entry's grams if you want it counted."""
    from app.routers.prints import log_print
    item.status = "failed"
    session.add(item)
    if not session.exec(select(PrintLog.id).where(PrintLog.queue_item_id == item.id, PrintLog.outcome == "failed")).first():
        from app import slots, spoolman
        used = partial_parts(session, item, progress)
        main = max(used, key=lambda p: p[1])[0] if used else slots.effective_filament(session, item)
        log = log_print(session, item.model_id, filament_id=main, grams=round(sum(g for _, g in used), 1) if used else None,
                        minutes=round(minutes, 1) if minutes else None, notes=item.notes, deduct=len(used) == 1, source="queue", queue_item_id=item.id,
                        outcome="failed", failure_reason=reason, printer_id=printer_id or item.printer_id)
        if len(used) > 1:                                              # several spools: each loses its share
            for spool_id, grams in used:
                spool = session.get(Filament, spool_id)
                if spool:
                    spool.remaining_g = max(0.0, spool.remaining_g - grams)
                    session.add(spool)
            log.uses = json.dumps([{"filament_id": sid, "grams": g} for sid, g in used])
        for spool_id, grams in used:
            spool = session.get(Filament, spool_id)
            if spool:
                spoolman.report_usage(session, spool, grams)


def _on_done(session: Session, item: QueueItem, printer_id: Optional[int] = None) -> None:
    """Deduct consumed filament exactly once, the moment a job transitions into "done",
    and write it to the model's print log (once per queue entry)."""
    from app import slots
    parts = slots.spool_parts(session, item)                              # the spool(s) in the entry's slot(s)
    for spool_id, grams in parts:
        spool = session.get(Filament, spool_id)
        if spool:
            spool.remaining_g = max(0.0, spool.remaining_g - grams)
            session.add(spool)
            from app import spoolman
            spoolman.report_usage(session, spool, grams)
    spool_id = max(parts, key=lambda p: p[1])[0] if parts else slots.effective_filament(session, item)
    from app.routers.prints import log_print
    for earlier in session.exec(select(PrintLog).where(PrintLog.queue_item_id == item.id, PrintLog.outcome == "failed")).all():
        earlier.queue_item_id = None                       # a failed first attempt stays in the log, apart from this success
        session.add(earlier)
    if not session.exec(select(PrintLog.id).where(PrintLog.queue_item_id == item.id)).first():
        log = log_print(session, item.model_id, filament_id=spool_id, grams=round(sum(g for _, g in parts), 1) if parts else item.estimated_grams,
                        minutes=item.actual_minutes or item.estimated_minutes, notes=item.notes, deduct=False, source="queue",
                        queue_item_id=item.id, measured=bool(item.actual_minutes), printer_id=printer_id or item.printer_id)
        if len(parts) > 1:
            log.uses = json.dumps([{"filament_id": sid, "grams": g} for sid, g in parts])


def complete_item(session: Session, item: QueueItem, minutes: Optional[float] = None, printer_id: Optional[int] = None) -> None:
    """Mark a queue entry done (as a printer reporting a finished print does)."""
    if item.status == "done":
        return
    item.status = "done"
    if minutes:
        item.actual_minutes = round(minutes, 1)
    if minutes and not item.estimated_minutes:
        item.estimated_minutes = round(minutes, 1)
        item.estimate_basis = None
    session.add(item)
    _on_done(session, item, printer_id)


@router.get("/uses-from-file/{model_id}")
def uses_from_file(model_id: int, printer_id: Optional[int] = None, session: Session = Depends(get_session)):
    """The filaments of the model's newest kept sliced file, each with the slot of the printer that holds the same kind of filament
    (or just its own number), so a multicolour job can be queued with every colour counted."""
    from app import slots
    from app.models import PrintFile, Printer
    rows = session.exec(select(PrintFile).where(PrintFile.model_id == model_id, PrintFile.filaments.is_not(None))
                        .order_by(PrintFile.created_at.desc(), PrintFile.id.desc())).all()
    if not rows:
        raise HTTPException(404, "No kept sliced file of this model lists its filaments (a sliced .3mf, or G-code from PrusaSlicer, OrcaSlicer or Bambu Studio)")
    file = rows[0]
    try:
        filaments = json.loads(file.filaments)
    except ValueError:
        filaments = []
    printer = session.get(Printer, printer_id) if printer_id else None
    overview = next((p for p in slots.overview(session) if p["id"] == printer_id), None) if printer else None
    taken, out = set(), []
    for f in filaments:
        suggestion = None
        for slot in (overview["slots"] if overview else []):
            spool = slot["spool"]
            if spool and slot["slot"] not in taken and str(spool["material"] or "").lower() == str(f.get("type") or "").lower() and slot["slot"] not in taken:
                a, b = slots._rgb(f.get("color")), slots._rgb(spool.get("color_hex"))
                if not (a and b) or sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5 <= 90:
                    suggestion = slot["slot"]
                    break
        if suggestion is None and overview and f["index"] <= overview["slot_count"] and f["index"] not in taken:
            suggestion = f["index"]
        if suggestion:
            taken.add(suggestion)
        out.append({**f, "suggested_slot": suggestion})
    return {"file": {"id": file.id, "filename": file.filename}, "filaments": out}


@router.get("/suggestions")
def printer_suggestions(session: Session = Depends(get_session)):
    """For waiting entries without a printer: the printers that would suit them, best first, and why."""
    from app import routing
    return routing.suggestions(session)


@router.post("/auto-assign")
def auto_assign(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Give waiting entries that have no printer the best printer (and slot) suggested for them. dry_run only shows the choice;
    a real run can be undone from Recent changes."""
    from app import activity, routing
    ids = payload.get("item_ids")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise HTTPException(400, "item_ids must be a list of queue entry ids")
    dry = payload.get("dry_run") is True
    chosen = {i: c[0] for i, c in routing.suggestions(session, ids).items()}
    old = {}
    if not dry:
        for item_id, best in chosen.items():
            item = session.get(QueueItem, item_id)
            old[str(item_id)] = {"printer_id": item.printer_id, "slot": item.slot, "filament_id": item.filament_id, "new_printer": best["printer_id"]}
            item.printer_id, item.slot = best["printer_id"], best["slot"]
            if best["slot"] and not item.filament_id:
                from app import slots as slot_mod
                item.filament_id = slot_mod.loaded(session, best["printer_id"], best["slot"])
            session.add(item)
        session.commit()
    activity_id = None
    if not dry and chosen:
        entry = activity.record(session, activity.actor_of(request), "queue", f"Gave {len(chosen)} waiting print(s) a printer",
                                undo={"kind": "queue_assign", "old": old})
        activity_id = entry.id
    return {"assigned": [{"id": i, "printer_id": b["printer_id"], "printer": b["name"], "slot": b["slot"], "reasons": b["reasons"]} for i, b in chosen.items()],
            "dry_run": dry, "activity_id": activity_id}


@router.get("/summary")
def summary(session: Session = Depends(get_session)):
    """Per printer: how many jobs are waiting or running for it and how long they will take (from the estimates)."""
    items = session.exec(select(QueueItem).where(QueueItem.status.in_(["queued", "printing"]))).all()
    names = {p.id: p.name for p in session.exec(select(Printer)).all()}
    rows = {}
    for i in items:
        key = i.printer_id if i.printer_id in names else None
        row = rows.setdefault(key, {"printer_id": key, "printer": names.get(key) if key else "Not assigned", "jobs": 0, "printing": 0,
                                    "minutes": 0.0, "without_estimate": 0})
        row["jobs"] += 1
        row["printing"] += 1 if i.status == "printing" else 0
        if i.estimated_minutes:
            row["minutes"] += i.estimated_minutes
        else:
            row["without_estimate"] += 1
    ordered = sorted(rows.values(), key=lambda r: (r["printer_id"] is None, r["printer"]))
    return {"printers": [{**r, "minutes": round(r["minutes"])} for r in ordered]}


@router.post("/{item_id}/send")
def send_item(item_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    """Send this queue entry to its printer: the model's newest kept G-code file. start=true also starts the print.
    (Administrator only; see app.auth.)"""
    from app import print_files, printers as printing
    item = session.get(QueueItem, item_id)
    if not item:
        raise HTTPException(404, "Not found")
    printer = session.get(Printer, item.printer_id) if item.printer_id else None
    if not printer:
        raise HTTPException(400, "Choose a printer for this job first")
    if item.status not in ("queued", "failed"):
        raise HTTPException(409, f"This job is already {item.status}")
    kept = [f for f in session.exec(select(PrintFile).where(PrintFile.model_id == item.model_id)
                                    .order_by(PrintFile.created_at.desc(), PrintFile.id.desc())).all() if f.kind in print_files.GCODE_KINDS]
    path = print_files.stored_path(kept[0].stored_name) if kept else None
    if not kept or not path.is_file():
        raise HTTPException(400, "Keep a sliced G-code file with this model first (its page, Sliced files)")
    start = payload.get("start") is True
    if start and payload.get("force") is not True:
        from app import stagger
        wait = stagger.reason_to_wait(session, printer)
        if wait:
            raise HTTPException(409, "Wait: " + wait)
    state = printing.status(printer.kind, printer.url, printer.api_key, printer.serial)
    if not state["online"]:
        raise HTTPException(502, state.get("message") or "The printer cannot be reached")
    if state["state"] == "printing":
        raise HTTPException(409, f"{printer.name} is busy printing {state.get('file') or 'something'}")
    name = printing.safe_gcode_name(kept[0].filename)
    try:
        result = printing.send_file(printer.kind, printer.url, printer.api_key, path, name, start)
    except printing.PrinterError as e:
        raise HTTPException(502, str(e))
    session.add(PrinterJob(printer_id=printer.id, filename=result["filename"], model_id=item.model_id, print_file_id=kept[0].id,
                           started=bool(result["started"])))
    if result["started"]:
        item.status = "printing"
        session.add(item)
    session.commit()
    return {**result, "status": item.status}


@router.post("/again/{model_id}")
def print_again(model_id: int, session: Session = Depends(get_session)):
    """Queue the model again with the filament, grams and time of its most recent print."""
    if not session.get(Model3D, model_id):
        raise HTTPException(404, "Model not found")
    from app import print_outcomes
    last = session.exec(select(PrintLog).where(PrintLog.model_id == model_id, print_outcomes.ok())
                        .order_by(PrintLog.printed_at.desc(), PrintLog.id.desc())).first()
    top = session.exec(select(QueueItem).order_by(QueueItem.position.desc())).first()
    item = QueueItem(model_id=model_id, position=(top.position + 1) if top else 0,
                     filament_id=last.filament_id if last else None, printer_id=last.printer_id if last else None,
                     estimated_grams=last.grams if last else None, estimated_minutes=last.minutes if last else None,
                     estimate_basis=("history" if last.measured else "estimate") if last and last.minutes else None,
                     notes="Printed again" if last else None)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.patch("/{item_id}")
def update_queue_item(item_id: int, payload: dict, session: Session = Depends(get_session)):
    item = session.get(QueueItem, item_id)
    if not item:
        raise HTTPException(404, "Not found")

    was_done = item.status == "done"
    was_failed = item.status == "failed"
    from app.routers.prints import clean_reason
    reason = clean_reason(payload.get("failure_reason")) if payload.get("status") == "failed" else None
    if "printer_id" in payload and payload["printer_id"] is not None and not session.get(Printer, payload["printer_id"]):
        raise HTTPException(400, "That printer does not exist")
    if "planned_date" in payload:
        item.planned_date = clean_date(payload["planned_date"])
    new_printer = payload.get("printer_id", item.printer_id)
    if "slot" in payload or (new_printer != item.printer_id and item.slot):
        wanted = payload["slot"] if "slot" in payload else None                # a new printer drops the old printer's slot
        item.slot = check_slot(session, new_printer, wanted)
        if item.slot and "filament_id" not in payload:
            from app import slots
            item.filament_id = slots.loaded(session, new_printer, item.slot) or item.filament_id
    if "uses" in payload:
        item.uses = check_uses(session, payload.get("printer_id", item.printer_id), payload["uses"])
        if item.uses and "estimated_grams" not in payload:
            item.estimated_grams = round(sum(u["grams"] for u in json.loads(item.uses)), 1)
    for field in ("status", "position", "filament_id", "notes", "estimated_grams", "estimated_minutes", "printer_id"):
        if field in payload:
            setattr(item, field, payload[field])
    if "estimated_minutes" in payload:
        item.estimate_basis = "manual" if item.estimated_minutes else None

    if item.status == "done" and not was_done:
        _on_done(session, item)
    elif item.status == "failed" and not was_failed:
        fail_item(session, item, reason)

    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.delete("/{item_id}")
def remove_queue_item(item_id: int, session: Session = Depends(get_session)):
    item = session.get(QueueItem, item_id)
    if not item:
        raise HTTPException(404, "Not found")
    # SQLite hands a deleted id to the next entry; a print log still pointing at it would make that entry look already logged
    for log in session.exec(select(PrintLog).where(PrintLog.queue_item_id == item_id)).all():
        log.queue_item_id = None
        session.add(log)
    session.delete(item)
    session.commit()
    return {"status": "deleted"}
