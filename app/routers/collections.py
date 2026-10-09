import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.db import get_session
from app.models import Collection, Model3D, SmartCollection
from app.smart_collections import resolve_smart_collection

router = APIRouter(prefix="/api/collections", tags=["collections"])


def _cover(session: Session, c: Collection) -> Optional[str]:
    """The thumbnail that stands for a collection: its chosen cover, else its first model that has a picture."""
    if c.cover_model_id:
        chosen = session.get(Model3D, c.cover_model_id)
        if chosen and chosen.thumbnail_path:
            return chosen.thumbnail_path
    return next((m.thumbnail_path for m in sorted(c.models, key=lambda m: m.id) if m.thumbnail_path), None)


@router.get("")
def list_collections(session: Session = Depends(get_session)):
    return [{**c.model_dump(), "models": len(c.models), "cover": _cover(session, c)} for c in session.exec(select(Collection)).all()]


@router.patch("/{collection_id}")
def update_collection(collection_id: int, payload: dict, session: Session = Depends(get_session)):
    """Choose the model whose picture stands for the collection (null goes back to the first model's picture)."""
    c = session.get(Collection, collection_id)
    if not c:
        raise HTTPException(404, "Not found")
    if "cover_model_id" in payload:
        value = payload["cover_model_id"]
        if value is None:
            c.cover_model_id = None
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not any(m.id == value for m in c.models):
                raise HTTPException(400, "The cover must be a model that is in the collection")
            c.cover_model_id = value
    session.add(c)
    session.commit()
    session.refresh(c)
    return {**c.model_dump(), "models": len(c.models), "cover": _cover(session, c)}


@router.post("")
def create_collection(payload: dict, session: Session = Depends(get_session)):
    c = Collection(name=payload["name"], parent_id=payload.get("parent_id"))
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


@router.post("/{collection_id}/models/{model_id}")
def add_model_to_collection(collection_id: int, model_id: int, session: Session = Depends(get_session)):
    collection = session.get(Collection, collection_id)
    model = session.get(Model3D, model_id)
    if not collection or not model:
        raise HTTPException(404, "Not found")
    if model not in collection.models:
        collection.models.append(model)
        session.add(collection)
        session.commit()
    return collection


@router.delete("/{collection_id}/models/{model_id}")
def remove_model_from_collection(collection_id: int, model_id: int, session: Session = Depends(get_session)):
    collection = session.get(Collection, collection_id)
    model = session.get(Model3D, model_id)
    if not collection or not model:
        raise HTTPException(404, "Not found")
    if model in collection.models:
        collection.models.remove(model)
        if collection.cover_model_id == model.id:
            collection.cover_model_id = None
        session.add(collection)
        session.commit()
    return {"status": "removed"}


@router.delete("/{collection_id}")
def delete_collection(collection_id: int, session: Session = Depends(get_session)):
    c = session.get(Collection, collection_id)
    if not c:
        raise HTTPException(404, "Not found")
    from app.models import ShareLink
    for share in session.exec(select(ShareLink).where(ShareLink.kind == "collection", ShareLink.target_id == collection_id)).all():
        session.delete(share)                       # SQLite reuses ids: a link must never outlive its collection
    session.delete(c)
    session.commit()
    return {"status": "deleted"}


# --- Smart (rule-based) collections ---

@router.get("/smart")
def list_smart_collections(session: Session = Depends(get_session)):
    return session.exec(select(SmartCollection)).all()


@router.post("/smart")
def create_smart_collection(payload: dict, session: Session = Depends(get_session)):
    sc = SmartCollection(name=payload["name"], rule_json=json.dumps(payload["rule"]))
    session.add(sc)
    session.commit()
    session.refresh(sc)
    return sc


@router.get("/smart/{smart_id}/models")
def get_smart_collection_models(smart_id: int, session: Session = Depends(get_session)):
    sc = session.get(SmartCollection, smart_id)
    if not sc:
        raise HTTPException(404, "Not found")
    return resolve_smart_collection(session, sc)


@router.delete("/smart/{smart_id}")
def delete_smart_collection(smart_id: int, session: Session = Depends(get_session)):
    sc = session.get(SmartCollection, smart_id)
    if not sc:
        raise HTTPException(404, "Not found")
    session.delete(sc)
    session.commit()
    return {"status": "deleted"}
