import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app import printers as printing
from app.config import LIBRARY_PATH
from app.db import get_session
from app.models import Model3D, Printer, PrinterJob

router = APIRouter(prefix="/api/printers", tags=["printers"])

MAX_PRINTERS = 20


def _json(printer: Printer) -> dict:
    return {"id": printer.id, "name": printer.name, "kind": printer.kind, "url": printer.url,
            "has_key": bool(printer.api_key), "created_at": printer.created_at}


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
    if existing is None or "url" in payload:
        try:
            out["url"] = printing.clean_url(payload.get("url"))
        except printing.PrinterError as e:
            raise HTTPException(400, str(e))
    if "api_key" in payload:                       # "" clears it; leaving it out keeps the current one
        key = payload["api_key"]
        if key is not None and not isinstance(key, str):
            raise HTTPException(400, "api_key must be text")
        out["api_key"] = (key or "").strip() or None
    kind = out.get("kind") or (existing.kind if existing else None)
    key_now = out["api_key"] if "api_key" in out else (existing.api_key if existing else None)
    if kind == "octoprint" and not key_now:
        raise HTTPException(400, "OctoPrint needs its API key (OctoPrint settings, API)")
    return out


@router.get("")
def list_printers(session: Session = Depends(get_session)):
    rows = session.exec(select(Printer).order_by(Printer.name)).all()
    return {"printers": [_json(p) for p in rows], "slicer_ready": printing.slicer_ready(), "slicer_note": printing.slicer_note()}


@router.post("")
def add_printer(payload: dict, session: Session = Depends(get_session)):
    if len(session.exec(select(Printer.id)).all()) >= MAX_PRINTERS:
        raise HTTPException(400, f"At most {MAX_PRINTERS} printers")
    printer = Printer(**_fields(payload))
    session.add(printer)
    session.commit()
    session.refresh(printer)
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
def delete_printer(printer_id: int, session: Session = Depends(get_session)):
    session.delete(_get(session, printer_id))
    session.commit()
    return {"status": "deleted"}


@router.get("/{printer_id}/status")
def printer_status(printer_id: int, session: Session = Depends(get_session)):
    printer = _get(session, printer_id)
    return printing.status(printer.kind, printer.url, printer.api_key)


@router.post("/{printer_id}/send")
def send(printer_id: int, file: Optional[UploadFile] = File(None), model_id: Optional[int] = Form(None),
         start: bool = Form(False), infill: float = Form(0.15), session: Session = Depends(get_session)):
    """Send a G-code file (uploaded) or a model (sliced here first) to the printer. start=true also starts the print."""
    printer = _get(session, printer_id)
    if file is None and model_id is None:
        raise HTTPException(400, "Choose a G-code file, or a model to slice")
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
                                   started=bool(result["started"])))
            session.commit()
            return result
    except printing.PrinterError as e:
        raise HTTPException(502, str(e))
