import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app import formats
from app.config import CONFIG_PATH, MODEL_EXTENSIONS
from app.db import get_session
from app.models import Model3D, ModelAttachment

router = APIRouter(prefix="/api/attachments", tags=["attachments"])
DIR = CONFIG_PATH / "attachments"
MAX_BYTES = 512 * 1024 * 1024
MAX_PER_MODEL = 50


def stored_path(name: str) -> Path:
    return DIR / name


def _json(row: ModelAttachment) -> dict:
    return {**row.model_dump(exclude={"stored_name"}), **formats.describe_attachment(row.kind), "file_exists": stored_path(row.stored_name).is_file()}


@router.get("/formats")
def format_table():
    """What Model Hub does with each kind of file."""
    return formats.table()


@router.get("")
def list_attachments(model_id: int, session: Session = Depends(get_session)):
    rows = session.exec(select(ModelAttachment).where(ModelAttachment.model_id == model_id).order_by(ModelAttachment.id)).all()
    return [_json(r) for r in rows]


@router.post("")
def upload(model_id: int = Form(...), file: UploadFile = File(...), notes: Optional[str] = Form(None), session: Session = Depends(get_session)):
    """Keep a file with a model that Model Hub does not open (a CAD project, a slicer project, a scene). Model files go through Import."""
    if not session.get(Model3D, model_id):
        raise HTTPException(404, "Model not found")
    name = formats.safe_name(file.filename or "file")
    ext = formats.extension_of(name)
    if ext in formats.DENIED:
        raise HTTPException(400, f"A .{ext} file cannot be kept here")
    if "." + ext in MODEL_EXTENSIONS:
        raise HTTPException(400, "That is a model file: add it with Import, then group it with this model as a version")
    if len(session.exec(select(ModelAttachment.id).where(ModelAttachment.model_id == model_id)).all()) >= MAX_PER_MODEL:
        raise HTTPException(400, f"At most {MAX_PER_MODEL} files per model")
    DIR.mkdir(parents=True, exist_ok=True)
    stored = uuid.uuid4().hex + (("." + ext) if ext.isalnum() and len(ext) <= 8 else "")
    path = stored_path(stored)
    size = 0
    try:
        with open(path, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, "That file is larger than 512 MB")
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "That file is empty")
        row = ModelAttachment(model_id=model_id, filename=name, stored_name=stored, kind=ext, size_bytes=size, notes=(notes or "").strip()[:500] or None)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _json(row)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@router.get("/{attachment_id}/download")
def download(attachment_id: int, session: Session = Depends(get_session)):
    row = session.get(ModelAttachment, attachment_id)
    path = stored_path(row.stored_name) if row else None
    if not row or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=row.filename, media_type="application/octet-stream", headers={"X-Content-Type-Options": "nosniff"})


@router.patch("/{attachment_id}")
def update(attachment_id: int, payload: dict, session: Session = Depends(get_session)):
    row = session.get(ModelAttachment, attachment_id)
    if not row:
        raise HTTPException(404, "Not found")
    if "notes" in payload:
        if payload["notes"] is not None and not isinstance(payload["notes"], str):
            raise HTTPException(400, "notes must be text")
        row.notes = (payload["notes"] or "").strip()[:500] or None
    session.add(row)
    session.commit()
    session.refresh(row)
    return _json(row)


@router.delete("/{attachment_id}")
def delete(attachment_id: int, session: Session = Depends(get_session)):
    row = session.get(ModelAttachment, attachment_id)
    if not row:
        raise HTTPException(404, "Not found")
    stored_path(row.stored_name).unlink(missing_ok=True)
    session.delete(row)
    session.commit()
    return {"status": "deleted"}
