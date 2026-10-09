import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import Model3D, PrintProfile

router = APIRouter(prefix="/api/profiles", tags=["profiles"])
MAX_PROFILES = 100


def clean_settings(payload) -> dict:
    """Only the known print-setting fields, as short text; empty ones are dropped."""
    from app.routers.library import PRINT_SETTING_FIELDS
    if not isinstance(payload, dict):
        raise HTTPException(400, "settings must be the print settings")
    out = {}
    for key, value in payload.items():
        if key not in PRINT_SETTING_FIELDS:
            raise HTTPException(400, f"Unknown print setting: {str(key)[:40]}")
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise HTTPException(400, f"{key} must be text or a number")
        text = str(value).strip()
        if len(text) > PRINT_SETTING_FIELDS[key]:
            raise HTTPException(400, f"{key} is too long")
        if text:
            out[key] = text
    return out


def _name(session: Session, value, own_id=None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "Give the profile a name")
    name = value.strip()[:60]
    for other in session.exec(select(PrintProfile)).all():
        if other.id != own_id and other.name.lower() == name.lower():
            raise HTTPException(400, "A profile with that name already exists")
    return name


def _json(p: PrintProfile) -> dict:
    try:
        settings = json.loads(p.settings)
    except ValueError:
        settings = {}
    return {"id": p.id, "name": p.name, "settings": settings if isinstance(settings, dict) else {}}


@router.get("")
def list_profiles(session: Session = Depends(get_session)):
    return {"profiles": [_json(p) for p in session.exec(select(PrintProfile).order_by(PrintProfile.name)).all()]}


@router.post("")
def create_profile(payload: dict, session: Session = Depends(get_session)):
    if len(session.exec(select(PrintProfile.id)).all()) >= MAX_PROFILES:
        raise HTTPException(400, f"At most {MAX_PROFILES} profiles")
    settings = clean_settings(payload.get("settings"))
    if not settings:
        raise HTTPException(400, "A profile needs at least one setting")
    p = PrintProfile(name=_name(session, payload.get("name")), settings=json.dumps(settings))
    session.add(p)
    session.commit()
    session.refresh(p)
    return _json(p)


@router.post("/from-model/{model_id}")
def profile_from_model(model_id: int, payload: dict, session: Session = Depends(get_session)):
    """Keep a model's "what worked" as a named profile."""
    from app.routers.library import parse_print_settings
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    settings = {k: v for k, v in parse_print_settings(model).items() if k != "notes"}
    if not settings:
        raise HTTPException(400, "Save some print settings on the model first")
    if len(session.exec(select(PrintProfile.id)).all()) >= MAX_PROFILES:
        raise HTTPException(400, f"At most {MAX_PROFILES} profiles")
    p = PrintProfile(name=_name(session, payload.get("name")), settings=json.dumps(clean_settings(settings)))
    session.add(p)
    session.commit()
    session.refresh(p)
    return _json(p)


@router.put("/{profile_id}")
def update_profile(profile_id: int, payload: dict, session: Session = Depends(get_session)):
    p = session.get(PrintProfile, profile_id)
    if not p:
        raise HTTPException(404, "Not found")
    if "name" in payload:
        p.name = _name(session, payload["name"], profile_id)
    if "settings" in payload:
        settings = clean_settings(payload["settings"])
        if not settings:
            raise HTTPException(400, "A profile needs at least one setting")
        p.settings = json.dumps(settings)
    session.add(p)
    session.commit()
    return _json(p)


@router.delete("/{profile_id}")
def delete_profile(profile_id: int, session: Session = Depends(get_session)):
    p = session.get(PrintProfile, profile_id)
    if not p:
        raise HTTPException(404, "Not found")
    session.delete(p)
    session.commit()
    return {"status": "deleted"}


@router.post("/{profile_id}/apply/{model_id}")
def apply_profile(profile_id: int, model_id: int, payload: dict, session: Session = Depends(get_session)):
    """Put a profile on a model. mode "fill" (the default) only fills settings the model has not got; "replace" overwrites the ones the profile has."""
    from app.routers.library import parse_print_settings
    p = session.get(PrintProfile, profile_id)
    model = session.get(Model3D, model_id)
    if not p or not model:
        raise HTTPException(404, "Not found")
    mode = payload.get("mode", "fill")
    if mode not in ("fill", "replace"):
        raise HTTPException(400, "mode must be fill or replace")
    have = parse_print_settings(model)
    for key, value in _json(p)["settings"].items():
        if mode == "replace" or not have.get(key):
            have[key] = value
    model.print_settings = json.dumps(have)
    model.updated_at = datetime.utcnow()
    session.add(model)
    session.commit()
    return {"print_settings": have}
