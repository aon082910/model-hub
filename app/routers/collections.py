import json
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.db import get_session
from app.models import Collection, Model3D, SmartCollection
from app.smart_collections import resolve_smart_collection

router = APIRouter(prefix="/api/collections", tags=["collections"])


@router.get("")
def list_collections(session: Session = Depends(get_session)):
    return session.exec(select(Collection)).all()


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
        session.add(collection)
        session.commit()
    return {"status": "removed"}


@router.delete("/{collection_id}")
def delete_collection(collection_id: int, session: Session = Depends(get_session)):
    c = session.get(Collection, collection_id)
    if not c:
        raise HTTPException(404, "Not found")
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
