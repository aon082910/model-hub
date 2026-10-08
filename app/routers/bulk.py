"""Changing many models at once: tag them, file them, put them in a project, set their designer or license."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import library_filters
from app.db import get_session
from app.models import (
    Collection, Model3D, ModelCollectionLink, ModelTagLink, Project, ProjectModelLink, QueueItem, Tag,
)

router = APIRouter(prefix="/api/bulk", tags=["bulk"])

MAX_MODELS = 5000
ACTIONS = ("add_tag", "remove_tag", "add_collection", "remove_collection", "add_project", "remove_project",
           "set_designer", "set_license", "queue")


def _ids(session: Session, payload: dict) -> list:
    ids = payload.get("ids")
    if ids is not None:
        if not (isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
            raise HTTPException(400, "ids must be a list of model ids")
        if len(ids) > MAX_MODELS:
            raise HTTPException(400, f"At most {MAX_MODELS} models at a time")
        return list(dict.fromkeys(ids))
    filters = payload.get("filters")
    if not isinstance(filters, dict):
        raise HTTPException(400, "Give ids, or the filters that pick the models")
    try:
        found = session.exec(select(Model3D.id).where(*library_filters.conditions(session, library_filters.parse_flags(filters)))
                             .order_by(Model3D.id).limit(MAX_MODELS + 1)).all()
    except library_filters.FilterError as e:
        raise HTTPException(400, str(e))
    if len(found) > MAX_MODELS:
        raise HTTPException(400, f"That is more than {MAX_MODELS} models; narrow the filters first")
    return list(found)


def _text(value, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, f"Give {label}")
    return value.strip()[:limit]


def _check_value(session: Session, action: str, value):
    """Validate (and tidy) the value an action needs, before anything is looked up or changed."""
    if action in ("add_tag", "remove_tag"):
        return _text(value, "a tag", 40).lower()
    if action in ("add_collection", "remove_collection"):
        if not isinstance(value, int) or isinstance(value, bool) or not session.get(Collection, value):
            raise HTTPException(400, "Choose an existing collection")
    elif action in ("add_project", "remove_project"):
        if not isinstance(value, int) or isinstance(value, bool) or not session.get(Project, value):
            raise HTTPException(400, "Choose an existing project")
    elif action in ("set_designer", "set_license"):
        if not isinstance(value, str):
            raise HTTPException(400, "Give the designer or license (an empty one clears it)")
        return value.strip()[:200]
    return value


def _existing(session: Session, ids: list) -> list:
    return list(session.exec(select(Model3D.id).where(Model3D.id.in_(ids))).all()) if ids else []


@router.post("")
def bulk_edit(payload: dict, session: Session = Depends(get_session)):
    """{action, value, ids | filters}. Returns {"changed": n, "unchanged": m}: unchanged were already so."""
    action = payload.get("action")
    if action not in ACTIONS:
        raise HTTPException(400, "action must be one of: " + ", ".join(ACTIONS))
    requested = _ids(session, payload)
    value = _check_value(session, action, payload.get("value"))
    ids = _existing(session, requested)
    if not ids:
        return {"changed": 0, "unchanged": 0, "models": 0}
    changed = 0

    if action in ("add_tag", "remove_tag"):
        name = value
        tag = session.exec(select(Tag).where(Tag.name == name)).first()
        if action == "add_tag" and not tag:
            tag = Tag(name=name, ai_generated=False)
            session.add(tag)
            session.flush()
        if tag:
            have = set(session.exec(select(ModelTagLink.model_id).where(ModelTagLink.tag_id == tag.id, ModelTagLink.model_id.in_(ids))).all())
            for mid in ids:
                if action == "add_tag" and mid not in have:
                    session.add(ModelTagLink(model_id=mid, tag_id=tag.id))
                    changed += 1
                elif action == "remove_tag" and mid in have:
                    session.delete(session.get(ModelTagLink, (mid, tag.id)))
                    changed += 1
    elif action in ("add_collection", "remove_collection"):
        have = set(session.exec(select(ModelCollectionLink.model_id).where(
            ModelCollectionLink.collection_id == value, ModelCollectionLink.model_id.in_(ids))).all())
        for mid in ids:
            if action == "add_collection" and mid not in have:
                session.add(ModelCollectionLink(model_id=mid, collection_id=value))
                changed += 1
            elif action == "remove_collection" and mid in have:
                session.delete(session.get(ModelCollectionLink, (mid, value)))
                changed += 1
    elif action in ("add_project", "remove_project"):
        have = set(session.exec(select(ProjectModelLink.model_id).where(
            ProjectModelLink.project_id == value, ProjectModelLink.model_id.in_(ids))).all())
        for mid in ids:
            if action == "add_project" and mid not in have:
                session.add(ProjectModelLink(model_id=mid, project_id=value))
                changed += 1
            elif action == "remove_project" and mid in have:
                session.delete(session.get(ProjectModelLink, (value, mid)))
                changed += 1
    elif action in ("set_designer", "set_license"):
        field = "designer" if action == "set_designer" else "license"
        new = value
        for model in session.exec(select(Model3D).where(Model3D.id.in_(ids))).all():
            if (getattr(model, field) or "") != new:
                setattr(model, field, new or None)
                session.add(model)
                changed += 1
    else:                                      # queue
        position = max([q for q in session.exec(select(QueueItem.position)).all()] or [-1]) + 1
        for mid in ids:
            session.add(QueueItem(model_id=mid, position=position))
            position += 1
            changed += 1
    session.commit()
    return {"changed": changed, "unchanged": len(ids) - changed, "models": len(ids)}
