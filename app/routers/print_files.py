import hashlib
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app import print_files
from app.db import get_session
from app.models import Model3D, PrintFile, Printer

router = APIRouter(prefix="/api/print-files", tags=["print-files"])

MAX_PER_MODEL = 50


def _json(row: PrintFile) -> dict:
    try:
        filaments = json.loads(row.filaments) if row.filaments else []
    except ValueError:
        filaments = []
    return {**row.model_dump(exclude={"stored_name", "filaments"}), "filament_list": filaments, "file_exists": print_files.stored_path(row.stored_name).is_file(),
            "sendable": row.kind in print_files.GCODE_KINDS}


def remove_files(session: Session, model_ids: list) -> None:
    """Delete these models' sliced files (rows and disk). Does not commit."""
    if not model_ids:
        return
    for row in session.exec(select(PrintFile).where(PrintFile.model_id.in_(model_ids))).all():
        print_files.stored_path(row.stored_name).unlink(missing_ok=True)
        session.delete(row)


@router.get("")
def list_files(model_id: int, session: Session = Depends(get_session)):
    rows = session.exec(select(PrintFile).where(PrintFile.model_id == model_id).order_by(PrintFile.created_at.desc(), PrintFile.id.desc())).all()
    return [_json(r) for r in rows]


@router.post("")
def upload(model_id: int = Form(...), file: UploadFile = File(...), notes: Optional[str] = Form(None), printer_id: Optional[int] = Form(None),
           session: Session = Depends(get_session)):
    """Keep a sliced file with a model. G-code (.gcode .gco .g .bgcode) or a sliced .3mf."""
    if not session.get(Model3D, model_id):
        raise HTTPException(404, "Model not found")
    if printer_id is not None and not session.get(Printer, printer_id):
        raise HTTPException(400, "That printer does not exist")
    kind = Path(file.filename or "").suffix.lower().lstrip(".")
    if kind not in print_files.KINDS:
        raise HTTPException(400, "Only G-code (.gcode .gco .g .bgcode) or a sliced .3mf can be kept here")
    if len(session.exec(select(PrintFile.id).where(PrintFile.model_id == model_id)).all()) >= MAX_PER_MODEL:
        raise HTTPException(400, f"At most {MAX_PER_MODEL} sliced files per model")
    print_files.PRINT_DIR.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.{kind}"
    path = print_files.stored_path(stored)
    digest, size = hashlib.sha256(), 0
    try:
        with open(path, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > print_files.MAX_BYTES:
                    raise HTTPException(413, "That file is larger than 1 GB")
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "That file is empty")
        same = session.exec(select(PrintFile).where(PrintFile.model_id == model_id, PrintFile.sha256 == digest.hexdigest(), PrintFile.printer_id == printer_id)).first()
        if same:
            path.unlink(missing_ok=True)
            return {**_json(same), "duplicate": True}
        meta = print_files.read_metadata(path, kind)
        name = Path(file.filename).name[:200]
        row = PrintFile(model_id=model_id, filename=name, stored_name=stored, kind=kind, size_bytes=size, sha256=digest.hexdigest(),
                        notes=(notes or "").strip()[:1000] or None, printer_id=printer_id, **meta)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _json(row)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@router.get("/{file_id}/download")
def download(file_id: int, session: Session = Depends(get_session)):
    row = session.get(PrintFile, file_id)
    path = print_files.stored_path(row.stored_name) if row else None
    if not row or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=row.filename, media_type="application/octet-stream")


@router.patch("/{file_id}")
def update(file_id: int, payload: dict, session: Session = Depends(get_session)):
    row = session.get(PrintFile, file_id)
    if not row:
        raise HTTPException(404, "Not found")
    if "printer_id" in payload:
        if payload["printer_id"] is not None and (isinstance(payload["printer_id"], bool) or not isinstance(payload["printer_id"], int) or not session.get(Printer, payload["printer_id"])):
            raise HTTPException(400, "That printer does not exist")
        row.printer_id = payload["printer_id"]
    if "notes" in payload:
        notes = payload["notes"]
        if notes is not None and not isinstance(notes, str):
            raise HTTPException(400, "notes must be text")
        row.notes = (notes or "").strip()[:1000] or None
    for field, limit in (("slicer", 80), ("filament_type", 40), ("layer_height", 20)):
        if field in payload:
            value = payload[field]
            if value is not None and not isinstance(value, str):
                raise HTTPException(400, f"{field} must be text")
            setattr(row, field, (value or "").strip()[:limit] or None)
    for field in ("est_minutes", "est_grams"):
        if field in payload:
            value = payload[field]
            if value in (None, ""):
                setattr(row, field, None)
            else:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{field} must be a number")
                if not (0 <= number <= 10_000_000):
                    raise HTTPException(400, f"{field} is out of range")
                setattr(row, field, number)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _json(row)


@router.delete("/{file_id}")
def delete(file_id: int, session: Session = Depends(get_session)):
    row = session.get(PrintFile, file_id)
    if not row:
        raise HTTPException(404, "Not found")
    print_files.stored_path(row.stored_name).unlink(missing_ok=True)
    session.delete(row)
    session.commit()
    return {"status": "deleted"}
