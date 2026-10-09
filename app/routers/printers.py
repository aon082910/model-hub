import os
import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlmodel import Session, select

from app import printers as printing
from app.config import LIBRARY_PATH
from app.db import get_session
from app import activity, print_files
from app.models import Model3D, Printer, PrinterJob, PrintFile

router = APIRouter(prefix="/api/printers", tags=["printers"])

MAX_PRINTERS = 20


def _json(printer: Printer) -> dict:
    return {"id": printer.id, "name": printer.name, "kind": printer.kind, "url": printer.url,
            "has_key": bool(printer.api_key), "snapshot_url": printer.snapshot_url, "slot_count": printer.slot_count or 0, "serial": printer.serial, "watch_failures": bool(printer.watch_failures), "pause_on_failure": bool(printer.pause_on_failure), "tags": [t for t in (printer.tags or "").split(",") if t], "plug_kind": printer.plug_kind, "plug_host": printer.plug_host,
            "bed_x": printer.bed_x, "bed_y": printer.bed_y, "bed_z": printer.bed_z, "created_at": printer.created_at}


def _get(session: Session, printer_id: int) -> Printer:
    printer = session.get(Printer, printer_id)
    if not printer:
        raise HTTPException(404, "Not found")
    return printer


def _fields(payload: dict, existing: Optional[Printer] = None) -> dict:
    out = {}
    if existing is None or "name" in payload:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "Give the printer a name")
        out["name"] = name.strip()[:80]
    if existing is None or "kind" in payload:
        if payload.get("kind") not in printing.KINDS:
            raise HTTPException(400, "kind must be one of: " + ", ".join(printing.KINDS))
        out["kind"] = payload["kind"]
    kind_now = out.get("kind") or (existing.kind if existing else None)
    if existing is None or "url" in payload or ("kind" in payload and payload["kind"] != (existing.kind if existing else None)):
        try:
            value = payload.get("url") if "url" in payload else (existing.url if existing else None)
            out["url"] = printing.clean_host(value) if kind_now == "bambu" else printing.clean_url(value)
        except printing.PrinterError as e:
            raise HTTPException(400, str(e))
    if kind_now == "bambu" and (existing is None or "serial" in payload or existing.kind != "bambu"):
        try:
            out["serial"] = printing.clean_serial(payload.get("serial", existing.serial if existing else None))
        except printing.PrinterError as e:
            raise HTTPException(400, str(e))
    if kind_now == "bambu" and "slot_count" not in payload and (existing is None or not existing.slot_count):
        out["slot_count"] = 4                                      # an AMS has four slots; change it if there are more units
    for axis in ("bed_x", "bed_y", "bed_z"):
        if axis in payload:
            value = payload[axis]
            if value in (None, ""):
                out[axis] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not 10 <= value <= 5000:
                raise HTTPException(400, f"{axis} must be a size in mm between 10 and 5000")
            else:
                out[axis] = float(value)
    if "slot_count" in payload:
        count = payload["slot_count"]
        if count in (None, ""):
            count = 0
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 16:
            raise HTTPException(400, "slot_count must be a number from 0 to 16")
        out["slot_count"] = count
    if "snapshot_url" in payload:                  # "" clears it
        try:
            out["snapshot_url"] = printing.clean_snapshot_url(payload["snapshot_url"])
        except printing.PrinterError as e:
            raise HTTPException(400, str(e))
    if "watch_failures" in payload:
        if not isinstance(payload["watch_failures"], bool):
            raise HTTPException(400, "watch_failures must be true or false")
        snapshot = out.get("snapshot_url") if "snapshot_url" in out else (existing.snapshot_url if existing else None)
        if payload["watch_failures"] and not snapshot:
            raise HTTPException(400, "Watching needs the printer's camera picture address first")
        out["watch_failures"] = payload["watch_failures"]
        if not payload["watch_failures"]:
            out["pause_on_failure"] = False
    if "pause_on_failure" in payload:
        if not isinstance(payload["pause_on_failure"], bool):
            raise HTTPException(400, "pause_on_failure must be true or false")
        watching = out["watch_failures"] if "watch_failures" in out else bool(existing and existing.watch_failures)
        if payload["pause_on_failure"] and not watching:
            raise HTTPException(400, "Switch watching on first: pausing is only done for a printer that is watched")
        out["pause_on_failure"] = payload["pause_on_failure"]
    if "tags" in payload:
        raw = payload["tags"]
        if isinstance(raw, str):
            raw = [t for t in raw.split(",")]
        if not isinstance(raw, list) or len(raw) > 8:
            raise HTTPException(400, "tags must be a list of at most 8 labels")
        clean = []
        for tag in raw:
            if not isinstance(tag, str) or not tag.strip():
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,23}", tag.strip()):
                raise HTTPException(400, "A tag is up to 24 letters, numbers, spaces, dashes or underscores")
            if tag.strip().lower() not in clean:
                clean.append(tag.strip().lower())
        out["tags"] = ",".join(clean) or None
    if "plug_kind" in payload or "plug_host" in payload:
        from app import plugs
        kind = payload["plug_kind"] if "plug_kind" in payload else (existing.plug_kind if existing else None)
        host = payload["plug_host"] if "plug_host" in payload else (existing.plug_host if existing else None)
        if kind in (None, "") and host in (None, ""):
            out["plug_kind"], out["plug_host"] = None, None
        else:
            if kind not in plugs.KINDS:
                raise HTTPException(400, "plug_kind must be tasmota or shelly")
            try:
                out["plug_host"] = printing.clean_host(host)
            except printing.PrinterError as e:
                raise HTTPException(400, str(e))
            out["plug_kind"] = kind
    if "api_key" in payload:                       # "" clears it; leaving it out keeps the current one
        key = payload["api_key"]
        if key is not None and not isinstance(key, str):
            raise HTTPException(400, "api_key must be text")
        out["api_key"] = (key or "").strip() or None
    kind = out.get("kind") or (existing.kind if existing else None)
    key_now = out["api_key"] if "api_key" in out else (existing.api_key if existing else None)
    if kind == "octoprint" and not key_now:
        raise HTTPException(400, "OctoPrint needs its API key (OctoPrint settings, API)")
    if kind == "bambu" and not key_now:
        raise HTTPException(400, "A Bambu printer needs its LAN access code (on the printer's screen: Settings, WLAN)")
    return out


@router.get("")
def list_printers(session: Session = Depends(get_session)):
    rows = session.exec(select(Printer).order_by(Printer.name)).all()
    return {"printers": [_json(p) for p in rows], "slicer_ready": printing.slicer_ready(), "slicer_note": printing.slicer_note()}


@router.post("")
def add_printer(payload: dict, request: Request, session: Session = Depends(get_session)):
    if len(session.exec(select(Printer.id)).all()) >= MAX_PRINTERS:
        raise HTTPException(400, f"At most {MAX_PRINTERS} printers")
    printer = Printer(**_fields(payload))
    session.add(printer)
    session.commit()
    session.refresh(printer)
    activity.record(session, activity.actor_of(request), "printer", f"Added the printer {printer.name}")
    return _json(printer)


@router.patch("/{printer_id}")
def update_printer(printer_id: int, payload: dict, session: Session = Depends(get_session)):
    printer = _get(session, printer_id)
    for key, value in _fields(payload, printer).items():
        setattr(printer, key, value)
    session.add(printer)
    session.commit()
    session.refresh(printer)
    return _json(printer)


@router.delete("/{printer_id}")
def delete_printer(printer_id: int, request: Request, session: Session = Depends(get_session)):
    printer = _get(session, printer_id)
    name = printer.name
    from app import slots
    slots.forget_printer(session, printer_id)
    from app import maintenance, sensors
    maintenance.forget_printer(session, printer_id)
    sensors.forget_printer(session, printer_id)
    for kept in session.exec(select(PrintFile).where(PrintFile.printer_id == printer_id)).all():
        kept.printer_id = None                         # a file made for a printer that is gone suits any printer again (ids are reused)
        session.add(kept)
    session.delete(printer)
    session.commit()
    activity.record(session, activity.actor_of(request), "printer", f"Removed the printer {name}")
    return {"status": "deleted"}


@router.post("/{printer_id}/snapshot")
def printer_snapshot(printer_id: int, session: Session = Depends(get_session)):
    """Take a picture from the printer's camera now (to check the camera address)."""
    from fastapi.responses import Response
    from app.sources import SourceError, shrink_image
    printer = _get(session, printer_id)
    if not printer.snapshot_url:
        raise HTTPException(400, "Enter the camera's picture address for this printer first")
    try:
        return Response(shrink_image(printing.fetch_snapshot(printer.snapshot_url)), media_type="image/jpeg")
    except (printing.PrinterError, SourceError) as e:
        raise HTTPException(502, str(e))


def _control_one(session: Session, printer: Printer, action: str, request: Request) -> dict:
    """Look at what the printer is doing, then pause (only if printing), resume (only if paused) or cancel (either). Raises HTTPException."""
    state = printing.status(printer.kind, printer.url, printer.api_key, printer.serial)
    if not state["online"]:
        raise HTTPException(502, state.get("message") or "The printer cannot be reached")
    running, paused = state["state"] == "printing", state["state"] == "paused"
    if (action == "pause" and not running) or (action == "resume" and not paused) or (action == "cancel" and not (running or paused)):
        raise HTTPException(409, f"{printer.name} is {state['state']}, so it cannot be told to {action}")
    try:
        printing.control(printer.kind, printer.url, printer.api_key, printer.serial, action)
    except printing.PrinterError as e:
        raise HTTPException(502, str(e))
    activity.record(session, activity.actor_of(request), "printer", f"{printer.name}: {action} sent" + (f" ({state['file']})" if state.get("file") else ""))
    return {"status": "sent", "action": action, "was": state["state"]}


MAX_BULK = 50


@router.post("/bulk-control")
def bulk_control(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Pause, resume or cancel on several printers at once: the ones you name (printer_ids), or every printer in a state (state: printing or paused) or with a tag.
    Each printer is checked on its own, so one that cannot be reached or is not in the right state does not stop the others."""
    from app import printwatch
    action = payload.get("action")
    if action not in printing.CONTROL_ACTIONS:
        raise HTTPException(400, "action must be pause, resume or cancel")
    everyone = session.exec(select(Printer).order_by(Printer.name)).all()
    chosen = {}
    ids, state, tag = payload.get("printer_ids"), payload.get("state"), payload.get("tag")
    if ids is None and state is None and tag is None:
        raise HTTPException(400, "Say which printers: printer_ids, a state or a tag")
    if ids is not None:
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise HTTPException(400, "printer_ids must be a list of printer ids")
        chosen.update({p.id: p for p in everyone if p.id in ids})
    if state is not None:
        if state not in ("printing", "paused"):
            raise HTTPException(400, "state must be printing or paused")
        chosen.update({p.id: p for p in everyone if (printwatch.latest.get(p.id) or {}).get("state") == state})
    if tag is not None:
        if not isinstance(tag, str) or not tag.strip():
            raise HTTPException(400, "tag must be text")
        chosen.update({p.id: p for p in everyone if tag.strip().lower() in (p.tags or "").split(",")})
    if len(chosen) > MAX_BULK:
        raise HTTPException(400, f"At most {MAX_BULK} printers at once")
    results = []
    for printer in chosen.values():
        try:
            done = _control_one(session, printer, action, request)
            results.append({"id": printer.id, "name": printer.name, "ok": True, "message": f"{action} sent", "was": done["was"]})
        except HTTPException as e:
            results.append({"id": printer.id, "name": printer.name, "ok": False, "message": str(e.detail)})
    return {"action": action, "results": results, "sent": sum(1 for r in results if r["ok"])}


@router.post("/{printer_id}/control")
def control_printer(printer_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    """Pause, resume or cancel what the printer is printing now. It first looks at what the printer is doing:
    a pause is only sent to a printer that is printing, a resume to one that is paused, a cancel to either."""
    printer = _get(session, printer_id)
    action = payload.get("action")
    if action not in printing.CONTROL_ACTIONS:
        raise HTTPException(400, "action must be pause, resume or cancel")
    return _control_one(session, printer, action, request)


@router.post("/{printer_id}/plug-test")
def plug_test(printer_id: int, session: Session = Depends(get_session)):
    """Read the smart plug's energy total now."""
    from app import plugs
    printer = _get(session, printer_id)
    if not printer.plug_kind or not printer.plug_host:
        raise HTTPException(400, "Give the printer its smart plug first")
    try:
        return {"total_kwh": round(plugs.read_total_kwh(printer.plug_kind, printer.plug_host), 3)}
    except plugs.PlugError as e:
        raise HTTPException(502, str(e))


@router.post("/{printer_id}/watch-test")
def watch_test(printer_id: int, session: Session = Depends(get_session)):
    """Take one picture from the camera and ask the vision model about it now (nothing is sent to you or the printer)."""
    from app import failure_watch
    printer = _get(session, printer_id)
    try:
        return failure_watch.check(session, printer)
    except failure_watch.WatchError as e:
        raise HTTPException(502, str(e))


@router.get("/{printer_id}/status")
def printer_status(printer_id: int, session: Session = Depends(get_session)):
    printer = _get(session, printer_id)
    result = printing.status(printer.kind, printer.url, printer.api_key, printer.serial)
    if printer.kind == "bambu":                                    # the slots panel reads what the AMS reports from here
        from app import printwatch
        printwatch.latest[printer.id] = {**result, "name": printer.name}
    return result


@router.post("/{printer_id}/send")
def send(printer_id: int, file: Optional[UploadFile] = File(None), model_id: Optional[int] = Form(None),
         print_file_id: Optional[int] = Form(None), start: bool = Form(False), infill: float = Form(0.15), force: bool = Form(False),
         session: Session = Depends(get_session)):
    """Send a G-code file (uploaded), a sliced file kept with a model, or a model (sliced here first) to the printer.
    start=true also starts the print."""
    printer = _get(session, printer_id)
    if start and not force:
        from app import stagger
        wait = stagger.reason_to_wait(session, printer)
        if wait:
            raise HTTPException(409, "Wait: " + wait + ". Send it without starting and start it on the printer, or change the limit in Settings")
    if file is None and model_id is None and print_file_id is None:
        raise HTTPException(400, "Choose a G-code file, a kept sliced file, or a model to slice")
    if not (0.0 <= infill <= 1.0):
        raise HTTPException(400, "infill must be between 0 and 1")
    try:
        with printing.temp_dir() as tmp:
            workdir = Path(tmp)
            if file is not None:
                suffix = Path(file.filename or "").suffix.lower()
                if suffix not in printing.GCODE_EXTENSIONS:
                    raise HTTPException(400, "That is not a G-code file (.gcode, .gco, .g or .bgcode)")
                name = printing.safe_gcode_name(file.filename)
                path = workdir / name
                written = 0
                with open(path, "wb") as out:
                    while chunk := file.file.read(1024 * 1024):
                        written += len(chunk)
                        if written > printing.MAX_UPLOAD_BYTES:
                            raise printing.PrinterError("That file is larger than 1 GB")
                        out.write(chunk)
            elif print_file_id is not None:
                kept = session.get(PrintFile, print_file_id)
                if not kept:
                    raise HTTPException(404, "That sliced file was not found")
                if kept.kind not in print_files.GCODE_KINDS:
                    raise HTTPException(400, "A sliced .3mf cannot be sent from here; send its G-code")
                path = print_files.stored_path(kept.stored_name)
                if not path.is_file():
                    raise HTTPException(404, "The file is missing from this server")
                model_id = kept.model_id
                name = printing.safe_gcode_name(kept.filename)
            else:
                model = session.get(Model3D, model_id)
                if not model:
                    raise HTTPException(404, "Model not found")
                source = LIBRARY_PATH / model.path
                if not source.is_file():
                    raise HTTPException(404, "The model's file is missing from the library folder")
                path = printing.slice_model(source, workdir, infill)
                name = printing.safe_gcode_name(model.filename)
            result = printing.send_file(printer.kind, printer.url, printer.api_key, path, name, start)
            session.add(PrinterJob(printer_id=printer.id, filename=result["filename"], model_id=model_id if file is None else None,
                                   print_file_id=print_file_id, started=bool(result["started"])))
            session.commit()
            return result
    except printing.PrinterError as e:
        raise HTTPException(502, str(e))
