from fastapi import APIRouter, Depends
from sqlmodel import Session
from app.db import get_session
from app.settings_store import all_settings, set_setting
from app.auth import RESERVED_SETTING_KEYS, ensure_extension_api_key
import secrets

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys never echoed back in plaintext to the frontend after being set
SECRET_KEYS = {"ai_api_key", "thingiverse_token"}


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
