"""Changing many models at once: tag them, file them, put them in a project, set their designer or license."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
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


def apply(session: Session, action: str, value, ids: list) -> dict:
    """Do one bulk action to these (existing) models. Returns {"changed_ids": [...], "old": {id: previous value}, "created": [queue ids]}.
    Does not commit."""
    changed, old, created = [], {}, []
    if action in ("add_tag", "remove_tag"):
        tag = session.exec(select(Tag).where(Tag.name == value)).first()
        if action == "add_tag" and not tag:
            tag = Tag(name=value, ai_generated=False)
            session.add(tag)
            session.flush()
        if tag:
            have = set(session.exec(select(ModelTagLink.model_id).where(ModelTagLink.tag_id == tag.id, ModelTagLink.model_id.in_(ids))).all())
            for mid in ids:
                if action == "add_tag" and mid not in have:
                    session.add(ModelTagLink(model_id=mid, tag_id=tag.id))
                    changed.append(mid)
                elif action == "remove_tag" and mid in have:
                    session.delete(session.get(ModelTagLink, (mid, tag.id)))
                    changed.append(mid)
    elif action in ("add_collection", "remove_collection"):
        have = set(session.exec(select(ModelCollectionLink.model_id).where(
            ModelCollectionLink.collection_id == value, ModelCollectionLink.model_id.in_(ids))).all())
        for mid in ids:
            if action == "add_collection" and mid not in have:
                session.add(ModelCollectionLink(model_id=mid, collection_id=value))
                changed.append(mid)
            elif action == "remove_collection" and mid in have:
                session.delete(session.get(ModelCollectionLink, (mid, value)))
                changed.append(mid)
    elif action in ("add_project", "remove_project"):
        have = set(session.exec(select(ProjectModelLink.model_id).where(
            ProjectModelLink.project_id == value, ProjectModelLink.model_id.in_(ids))).all())
        for mid in ids:
            if action == "add_project" and mid not in have:
                session.add(ProjectModelLink(model_id=mid, project_id=value))
                changed.append(mid)
            elif action == "remove_project" and mid in have:
                session.delete(session.get(ProjectModelLink, (value, mid)))
                changed.append(mid)
    elif action in ("set_designer", "set_license"):
        field = "designer" if action == "set_designer" else "license"
        for model in session.exec(select(Model3D).where(Model3D.id.in_(ids))).all():
            if (getattr(model, field) or "") != value:
                old[model.id] = getattr(model, field)
                setattr(model, field, value or None)
                session.add(model)
                changed.append(model.id)
    else:                                      # queue
        position = max([q for q in session.exec(select(QueueItem.position)).all()] or [-1]) + 1
        for mid in ids:
            item = QueueItem(model_id=mid, position=position)
            session.add(item)
            session.flush()
            created.append(item.id)
            position += 1
            changed.append(mid)
    return {"changed_ids": changed, "old": old, "created": created}


INVERSE = {"add_tag": "remove_tag", "remove_tag": "add_tag", "add_collection": "remove_collection",
           "remove_collection": "add_collection", "add_project": "remove_project", "remove_project": "add_project"}

LABELS = {"add_tag": "Added the tag", "remove_tag": "Removed the tag", "add_collection": "Added to a collection",
          "remove_collection": "Removed from a collection", "add_project": "Added to a project", "remove_project": "Removed from a project",
          "set_designer": "Set the designer", "set_license": "Set the license", "queue": "Added to the print queue"}


def undo_spec(action: str, value, result: dict) -> Optional[dict]:
    """What it takes to reverse a bulk action that changed result["changed_ids"] (None if nothing changed)."""
    if not result["changed_ids"]:
        return None
    spec = {"kind": "bulk", "action": action, "value": value, "ids": result["changed_ids"]}
    if action in ("set_designer", "set_license"):
        spec["old"] = {str(k): v for k, v in result["old"].items()}
    if action == "queue":
        spec["created"] = result["created"]
    return spec


def undo(session: Session, spec: dict) -> int:
    """Reverse a bulk action. Anything changed again since is left alone. Returns how many models were put back."""
    action, value, ids = spec["action"], spec.get("value"), [i for i in spec.get("ids", []) if isinstance(i, int)]
    ids = _existing(session, ids)
    if action in INVERSE:
        return len(apply(session, INVERSE[action], value, ids)["changed_ids"]) if ids else 0
    if action in ("set_designer", "set_license"):
        field = "designer" if action == "set_designer" else "license"
        restored = 0
        for model in session.exec(select(Model3D).where(Model3D.id.in_(ids))).all():
            if (getattr(model, field) or "") == (value or "") and str(model.id) in spec.get("old", {}):
                setattr(model, field, spec["old"][str(model.id)])
                session.add(model)
                restored += 1
        return restored
    if action == "queue":
        removed = 0
        for item in session.exec(select(QueueItem).where(QueueItem.id.in_([i for i in spec.get("created", []) if isinstance(i, int)]))).all():
            if item.status == "queued":
                session.delete(item)
                removed += 1
        return removed
    return 0


@router.post("")
def bulk_edit(payload: dict, request: Request, session: Session = Depends(get_session)):
    """{action, value, ids | filters}. Returns {"changed": n, "unchanged": m, "activity_id": id}: unchanged were already so;
    the activity entry can be undone."""
    action = payload.get("action")
    if action not in ACTIONS:
        raise HTTPException(400, "action must be one of: " + ", ".join(ACTIONS))
    requested = _ids(session, payload)
    value = _check_value(session, action, payload.get("value"))
    ids = _existing(session, requested)
    if not ids:
        return {"changed": 0, "unchanged": 0, "models": 0, "activity_id": None}
    result = apply(session, action, value, ids)
    session.commit()
    entry = None
    if result["changed_ids"]:
        from app import activity
        what = f"{LABELS[action]}{' ' + repr(value) if isinstance(value, str) and value else ''}"
        entry = activity.record(session, activity.actor_of(request), "bulk_edit",
                                f"{what}: {len(result['changed_ids'])} model{'s' if len(result['changed_ids']) != 1 else ''}",
                                undo=undo_spec(action, value, result))
    changed = len(result["changed_ids"])
    return {"changed": changed, "unchanged": len(ids) - changed, "models": len(ids), "activity_id": entry.id if entry else None}
