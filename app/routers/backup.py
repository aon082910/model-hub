import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app import backup
from app.config import CONFIG_PATH

router = APIRouter(prefix="/api/backup", tags=["backup"])

MAX_UPLOAD_BYTES = 4 * 1024 ** 3


def _download_name() -> str:
    return f"model-hub-backup-{backup._stamp()}.zip"


@router.get("")
def download_backup(background: BackgroundTasks):
    """A backup to download now: the database and the saved listing pictures (not the model files)."""
    handle, name = tempfile.mkstemp(suffix=".zip", dir=CONFIG_PATH)
    os.close(handle)
    path = Path(name)
    try:
        backup.make_backup(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    background.add_task(path.unlink, missing_ok=True)
    return FileResponse(path, media_type="application/zip", filename=_download_name())


@router.get("/saved")
def list_saved():
    return {"backups": backup.saved_backups()}


@router.post("/save")
def save_on_server():
    """Keep a copy in the server's config folder (which an Unraid appdata backup picks up)."""
    return backup.save_backup()


@router.get("/saved/{name}")
def download_saved(name: str):
    try:
        path = backup.saved_path(name)
    except backup.BackupError as e:
        raise HTTPException(404, str(e))
    return FileResponse(path, media_type="application/zip", filename=name)


@router.delete("/saved/{name}")
def delete_saved(name: str):
    try:
        backup.saved_path(name).unlink()
    except backup.BackupError as e:
        raise HTTPException(404, str(e))
    return {"status": "deleted"}


def _restore(path: Path) -> dict:
    try:
        return backup.restore_backup(path)
    except backup.BackupError as e:
        raise HTTPException(400, str(e))


@router.post("/restore")
def restore_uploaded(file: UploadFile = File(...), confirm: str = Form("")):
    """Replace the data with an uploaded backup. confirm must be "replace"."""
    if confirm != "replace":
        raise HTTPException(400, 'Send confirm=replace to restore (this replaces your current data)')
    handle, name = tempfile.mkstemp(suffix=".zip", dir=CONFIG_PATH)
    path = Path(name)
    try:
        written = 0
        with os.fdopen(handle, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "That file is larger than this server will restore")
                out.write(chunk)
        return _restore(path)
    finally:
        path.unlink(missing_ok=True)


@router.post("/saved/{name}/restore")
def restore_saved(name: str, payload: dict):
    if payload.get("confirm") != "replace":
        raise HTTPException(400, 'Send {"confirm": "replace"} to restore (this replaces your current data)')
    try:
        path = backup.saved_path(name)
    except backup.BackupError as e:
        raise HTTPException(404, str(e))
    return _restore(path)
