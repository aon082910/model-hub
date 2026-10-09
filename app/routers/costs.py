from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app import costing
from app.db import get_session
from app.models import Filament, Model3D

router = APIRouter(prefix="/api/costs", tags=["costs"])


@router.get("/quote")
def quote(model_id: Optional[int] = None, grams: Optional[float] = None, minutes: Optional[float] = None, filament_id: Optional[int] = None,
          quantity: int = 1, session: Session = Depends(get_session)):
    """Cost and suggested price. Give grams and minutes, or a model (its numbers are looked up and returned as "from").
    Anything you pass replaces what was looked up."""
    if not 1 <= quantity <= 100000:
        raise HTTPException(400, "quantity must be between 1 and 100000")
    start = {"grams": None, "minutes": None, "filament_id": None}
    if model_id is not None:
        if not session.get(Model3D, model_id):
            raise HTTPException(404, "Model not found")
        start = costing.defaults_for(session, model_id)
    grams = grams if grams is not None else start["grams"]
    minutes = minutes if minutes is not None else start["minutes"]
    filament_id = filament_id if filament_id is not None else start["filament_id"]
    if grams is None or minutes is None:
        raise HTTPException(400, "Give the grams and minutes (this model has no kept sliced file, print or estimate to take them from)")
    if not (0 <= grams <= 1_000_000 and 0 <= minutes <= 10_000_000):
        raise HTTPException(400, "grams and minutes must be sensible numbers")
    if filament_id is not None and not session.get(Filament, filament_id):
        raise HTTPException(400, "That filament spool does not exist")
    return {**costing.quote(session, grams, minutes, filament_id, quantity), "filament_id": filament_id, "from_model": start if model_id is not None else None}
