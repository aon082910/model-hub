from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import families
from app.db import get_session
from app.models import Model3D, ModelFamily

router = APIRouter(prefix="/api/families", tags=["families"])

MAX_GROUP = 50


def _label(value) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, "label must be text")
    return value.strip()[:40] or None


def _json(session: Session, family: ModelFamily) -> dict:
    return {"id": family.id, "name": family.name, "members": [
        {"id": m.id, "filename": m.filename, "version_label": m.version_label, "created_at": m.created_at}
        for m in families.members(session, family.id)]}


@router.post("")
def group(payload: dict, session: Session = Depends(get_session)):
    """Group models as versions of one another. Models already in a family bring their whole family along."""
    ids = payload.get("model_ids")
    if not (isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise HTTPException(400, "model_ids must be a list of model ids")
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2 or len(ids) > MAX_GROUP:
        raise HTTPException(400, f"Choose between 2 and {MAX_GROUP} models")
    found = session.exec(select(Model3D.id).where(Model3D.id.in_(ids))).all()
    if len(found) != len(ids):
        raise HTTPException(404, "One of those models was not found")
    name = payload.get("name")
    family = families.group(session, ids, _label(name) if name is not None else None)
    return _json(session, family)


@router.get("/suggest/{model_id}")
def suggest(model_id: int, session: Session = Depends(get_session)):
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    return {"suggestions": families.suggest(session, model)}


@router.get("/{family_id}")
def get_family(family_id: int, session: Session = Depends(get_session)):
    family = session.get(ModelFamily, family_id)
    if not family:
        raise HTTPException(404, "Not found")
    return _json(session, family)


@router.patch("/{family_id}")
def rename_family(family_id: int, payload: dict, session: Session = Depends(get_session)):
    family = session.get(ModelFamily, family_id)
    if not family:
        raise HTTPException(404, "Not found")
    family.name = _label(payload.get("name"))
    session.add(family)
    session.commit()
    return _json(session, family)


@router.put("/members/{model_id}")
def set_label(model_id: int, payload: dict, session: Session = Depends(get_session)):
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    if not model.family_id:
        raise HTTPException(400, "That model is not in a group of versions")
    model.version_label = _label(payload.get("label"))
    session.add(model)
    session.commit()
    return {"id": model.id, "version_label": model.version_label}


@router.delete("/members/{model_id}")
def leave(model_id: int, session: Session = Depends(get_session)):
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    family_id = model.family_id
    model.family_id, model.version_label = None, None
    session.add(model)
    session.flush()
    families.leave_families(session, {family_id})
    session.commit()
    return {"status": "left"}
