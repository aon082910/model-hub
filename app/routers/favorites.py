from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app.auth import current_user
from app.db import get_session
from app.models import Favorite, Model3D

router = APIRouter(prefix="/api/favorites", tags=["favorites"])


def owner_of(request: Request, session: Session) -> str:
    return (current_user(request, session) or {}).get("username") or ""


@router.get("")
def my_favorites(request: Request, session: Session = Depends(get_session)):
    """The ids of the models this login has starred."""
    owner = owner_of(request, session)
    return {"ids": list(session.exec(select(Favorite.model_id).where(Favorite.owner == owner).order_by(Favorite.id)).all())}


@router.put("/{model_id}")
def star(model_id: int, request: Request, session: Session = Depends(get_session)):
    if not session.get(Model3D, model_id):
        raise HTTPException(404, "Model not found")
    owner = owner_of(request, session)
    if not session.exec(select(Favorite.id).where(Favorite.owner == owner, Favorite.model_id == model_id)).first():
        session.add(Favorite(owner=owner, model_id=model_id))
        session.commit()
    return {"favorite": True}


@router.delete("/{model_id}")
def unstar(model_id: int, request: Request, session: Session = Depends(get_session)):
    owner = owner_of(request, session)
    for row in session.exec(select(Favorite).where(Favorite.owner == owner, Favorite.model_id == model_id)).all():
        session.delete(row)
    session.commit()
    return {"favorite": False}
