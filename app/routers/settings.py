import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session
from app.db import get_session
from app.settings_store import all_settings, set_setting
from app.auth import RESERVED_SETTING_KEYS, ensure_extension_api_key
from app.sources import secret_setting_keys
import secrets

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys never echoed back in plaintext to the frontend after being set
SECRET_KEYS = {"ai_api_key"} | secret_setting_keys()   # site tokens / API keys


@router.get("")
def get_settings(session: Session = Depends(get_session)):
    data = all_settings(session)
    for k in RESERVED_SETTING_KEYS:
        data.pop(k, None)
    for k in SECRET_KEYS:
        if data.get(k):
            data[k] = "********"
    return data


@router.put("")
def update_settings(payload: dict, session: Session = Depends(get_session)):
    for k, v in payload.items():
        if k in RESERVED_SETTING_KEYS:
            continue  # these have their own dedicated, more-restricted endpoints
        if v == "********":
            continue  # unchanged secret, skip
        set_setting(session, k, str(v))
    return {"status": "ok"}


@router.get("/extension-key")
def get_extension_key(session: Session = Depends(get_session)):
    """Session-cookie only (RESERVED_SETTING_KEYS keeps it out of GET /api/settings,
    and this route isn't in API_KEY_ALLOWED_PATHS, so the extension's own API key
    can't be used to read -- or rotate -- itself)."""
    return {"extension_api_key": ensure_extension_api_key(session)}


@router.post("/regenerate-extension-key")
def regenerate_extension_key(session: Session = Depends(get_session)):
    key = secrets.token_urlsafe(24)
    set_setting(session, "extension_api_key", key)
    return {"extension_api_key": key}


EXTENSION_DIR = Path(__file__).resolve().parent.parent.parent / "browser-extension"


@router.get("/extension.zip")
def download_extension():
    """The browser extension as a zip, so it can be installed from the server you
    already have open (it holds no secrets: the API key is typed into its popup)."""
    if not EXTENSION_DIR.is_dir():
        raise HTTPException(404, "The browser extension is not bundled with this install")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(EXTENSION_DIR).as_posix())
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="model-hub-extension.zip"'})
