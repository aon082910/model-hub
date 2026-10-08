"""Print times learned from your own prints (see app/learned.py)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app import learned
from app.db import get_session
from app.models import Model3D

router = APIRouter(prefix="/api/estimates", tags=["estimates"])


@router.get("/accuracy")
def accuracy(session: Session = Depends(get_session)):
    """How the estimates compare with the real print times so far."""
    return learned.accuracy(session)


@router.get("/{model_id}")
def for_model(model_id: int, base: Optional[float] = None, session: Session = Depends(get_session)):
    """The best guess for how long this model takes. base: a figure (minutes) from the slicer or the rough estimate
    to correct when the model has no history of its own."""
    if not session.get(Model3D, model_id):
        raise HTTPException(404, "Model not found")
    return learned.suggest(session, model_id, base)
