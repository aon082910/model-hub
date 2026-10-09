"""A printer that is not there: the part of the OctoPrint and Moonraker (Klipper) web interfaces that slicers use to send a file.

Off until switched on (Settings, Slicer). A slicer needs this server's address and an API token with write access (made in Settings, API tokens) as its API key."""
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app import print_files, slicer_inbox, version
from app.db import get_session
from app.models import Model3D, SlicerUpload
from app.settings_store import get_setting, set_setting

public_router = APIRouter(tags=["virtual-printer"], include_in_schema=False)       # the printer's own web interface (needs the API key)
router = APIRouter(prefix="/api/slicer-inbox", tags=["virtual-printer"])
settings_router = APIRouter(prefix="/api/settings/virtual-printer", tags=["virtual-printer"])      # administrator only (see app.auth)
NAME = "Model Hub"


def _on(session: Session) -> None:
    if not slicer_inbox.enabled(session):
        raise HTTPException(404, "Not found")


def _who(request: Request) -> Optional[str]:
    return (getattr(request.state, "user", None) or {}).get("username")


async def _spool(upload: UploadFile) -> Path:
    """Write the upload to a temporary file (never into memory), refusing anything over the size limit."""
    handle, name = tempfile.mkstemp(prefix="slicer-", suffix=".part", dir=str(print_files.PRINT_DIR.parent))
    size = 0
    try:
        with open(handle, "wb") as out:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > print_files.MAX_BYTES:
                    raise HTTPException(413, "That file is larger than 1 GB")
                out.write(chunk)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


async def _take(session: Session, request: Request, upload: UploadFile, print_flag: Optional[str]) -> SlicerUpload:
    _on(session)
    filename = Path(upload.filename or "").name
    try:
        slicer_inbox.kind_of(filename)
    except slicer_inbox.InboxError as e:
        raise HTTPException(415, str(e))
    path = await _spool(upload)
    try:
        return await run_in_threadpool(slicer_inbox.receive, session, path, filename, str(print_flag or "").lower() == "true", _who(request))
    except slicer_inbox.InboxError as e:
        raise HTTPException(400, str(e))
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- OctoPrint
@public_router.get("/api/version")
def octoprint_version(session: Session = Depends(get_session)):
    _on(session)
    return {"api": "0.1", "server": "1.9.3", "text": f"OctoPrint 1.9.3 ({NAME} {version.VERSION})"}


@public_router.post("/api/files/local")
async def octoprint_upload(request: Request, file: UploadFile = File(...), select: Optional[str] = Form(None), print: Optional[str] = Form(None),
                           session: Session = Depends(get_session)):
    row = await _take(session, request, file, print)
    name = row.filename
    return JSONResponse({"files": {"local": {"name": name, "path": name, "origin": "local", "refs": {"resource": f"/api/files/local/{name}"}}}, "done": True}, status_code=201)


# ---------------------------------------------------------------- Moonraker
@public_router.get("/server/info")
def moonraker_server_info(session: Session = Depends(get_session)):
    _on(session)
    return {"result": {"klippy_connected": True, "klippy_state": "ready", "components": [], "failed_components": [], "registered_directories": ["gcodes"],
                       "warnings": [], "websocket_count": 0, "moonraker_version": f"v0.8.0-{NAME.lower().replace(' ', '')}", "api_version": [1, 0, 5], "api_version_string": "1.0.5"}}


@public_router.get("/printer/info")
def moonraker_printer_info(session: Session = Depends(get_session)):
    _on(session)
    return {"result": {"state": "ready", "state_message": f"{NAME} (a virtual printer: files sent here are kept, never printed)", "hostname": "model-hub",
                       "software_version": f"v{version.VERSION}", "klipper_path": "/", "python_path": "/", "log_file": "", "config_file": ""}}


@public_router.post("/server/files/upload")
async def moonraker_upload(request: Request, file: UploadFile = File(...), root: Optional[str] = Form(None), path: Optional[str] = Form(None),
                           print: Optional[str] = Form(None), session: Session = Depends(get_session)):
    row = await _take(session, request, file, print)
    return {"item": {"path": row.filename, "root": "gcodes", "modified": time.time(), "size": row.size_bytes, "permissions": "rw"},
            "print_started": False, "print_queued": bool(row.print_requested), "action": "create_file"}


# ---------------------------------------------------------------- the inbox (for the page)
def _json(row: SlicerUpload, names: dict) -> dict:
    return {"id": row.id, "filename": row.filename, "kind": row.kind, "size_bytes": row.size_bytes, "status": row.status, "model_id": row.model_id, "model": names.get(row.model_id),
            "print_requested": row.print_requested, "sent_by": row.sent_by, "sent_at": row.sent_at.isoformat()}


@router.get("")
def list_inbox(session: Session = Depends(get_session)):
    rows = session.exec(select(SlicerUpload).order_by(SlicerUpload.id.desc()).limit(100)).all()
    names = {m.id: m.filename for m in session.exec(select(Model3D).where(Model3D.id.in_({r.model_id for r in rows if r.model_id} or {0}))).all()}
    return {"files": [_json(r, names) for r in rows], "waiting": sum(1 for r in rows if r.status == "waiting")}


@router.post("/{upload_id}/file")
def file_it(upload_id: int, payload: dict, session: Session = Depends(get_session)):
    """Keep a waiting file with a model (and queue it if the slicer asked to print)."""
    row = session.get(SlicerUpload, upload_id)
    model = session.get(Model3D, payload.get("model_id")) if isinstance(payload.get("model_id"), int) else None
    if not row:
        raise HTTPException(404, "Not found")
    if row.status != "waiting":
        raise HTTPException(409, "That file has already been filed")
    if not model:
        raise HTTPException(400, "Choose a model from the library")
    slicer_inbox.file_into(session, row, model)
    session.commit()
    return _json(row, {model.id: model.filename})


@router.post("/{upload_id}/new-model")
def new_model(upload_id: int, session: Session = Depends(get_session)):
    """A waiting sliced 3MF carries the model it was made from: add that as a new model and keep the file with it."""
    from app.scanner import import_uploaded_file
    row = session.get(SlicerUpload, upload_id)
    if not row:
        raise HTTPException(404, "Not found")
    if row.status != "waiting":
        raise HTTPException(409, "That file has already been filed")
    if row.kind != "3mf":
        raise HTTPException(400, "Only a sliced 3MF contains a model: G-code does not")
    path = print_files.stored_path(row.stored_name)
    try:
        model = import_uploaded_file(session, row.filename, path.read_bytes())
    except ValueError as e:
        raise HTTPException(400, str(e))
    slicer_inbox.file_into(session, row, model)
    session.commit()
    return _json(row, {model.id: model.filename})


@router.delete("/{upload_id}")
def delete_upload(upload_id: int, session: Session = Depends(get_session)):
    row = session.get(SlicerUpload, upload_id)
    if not row:
        raise HTTPException(404, "Not found")
    if row.stored_name:
        print_files.stored_path(row.stored_name).unlink(missing_ok=True)
    session.delete(row)
    session.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------- Settings
@settings_router.get("")
def get_virtual_printer(request: Request, session: Session = Depends(get_session)):
    base = str(request.base_url).rstrip("/")
    return {"enabled": slicer_inbox.enabled(session), "address": base,
            "how": [f"PrusaSlicer or OrcaSlicer: add a physical printer, host type OctoPrint (or Moonraker for Orca), address {base}, and an API token with write access as the API key.",
                    "Cura: install the OctoPrint Connection plugin, then add the address and the API key.",
                    "Press the slicer's Test button, then Upload (or Upload and print: the file is queued, never started)."]}


@settings_router.post("")
def set_virtual_printer(payload: dict, session: Session = Depends(get_session)):
    if not isinstance(payload.get("enabled"), bool):
        raise HTTPException(400, "enabled must be true or false")
    set_setting(session, "virtual_printer", "true" if payload["enabled"] else "")
    return {"enabled": payload["enabled"]}
