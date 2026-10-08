from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.db import get_session
from datetime import datetime

from app.models import Filament, FilamentPrice

router = APIRouter(prefix="/api/filament", tags=["filament"])


@router.get("")
def list_filament(session: Session = Depends(get_session)):
    return session.exec(select(Filament)).all()


@router.post("")
def create_filament(payload: dict, session: Session = Depends(get_session)):
    f = Filament(**payload)
    session.add(f)
    session.commit()
    session.refresh(f)
    _note_price(session, f)
    session.refresh(f)
    return f


@router.patch("/{filament_id}")
def update_filament(filament_id: int, payload: dict, session: Session = Depends(get_session)):
    f = session.get(Filament, filament_id)
    if not f:
        raise HTTPException(404, "Not found")
    old_cost = f.cost
    for k, v in payload.items():
        if hasattr(f, k):
            setattr(f, k, v)
    session.add(f)
    session.commit()
    session.refresh(f)
    if f.cost != old_cost:
        _note_price(session, f)
        session.refresh(f)
    return f


def _note_price(session: Session, f: Filament) -> None:
    """Remember what a spool cost when its price is set or changed."""
    if f.cost is not None:
        session.add(FilamentPrice(filament_id=f.id, cost=float(f.cost), spool_weight_g=f.spool_weight_g or 1000))
        session.commit()


@router.get("/{filament_id}/prices")
def price_history(filament_id: int, session: Session = Depends(get_session)):
    """Every price this spool has had, newest first, with the price per kilogram."""
    if not session.get(Filament, filament_id):
        raise HTTPException(404, "Not found")
    rows = session.exec(select(FilamentPrice).where(FilamentPrice.filament_id == filament_id)
                        .order_by(FilamentPrice.at.desc(), FilamentPrice.id.desc())).all()
    return [{"id": r.id, "cost": r.cost, "at": r.at, "per_kg": round(r.cost / (r.spool_weight_g or 1000) * 1000, 2)} for r in rows]


@router.post("/{filament_id}/consume")
def consume_filament(filament_id: int, payload: dict, session: Session = Depends(get_session)):
    f = session.get(Filament, filament_id)
    if not f:
        raise HTTPException(404, "Not found")
    grams = float(payload.get("grams", 0))
    f.remaining_g = max(0.0, f.remaining_g - grams)
    session.add(f)
    session.commit()
    session.refresh(f)
    return f


@router.delete("/{filament_id}")
def delete_filament(filament_id: int, session: Session = Depends(get_session)):
    f = session.get(Filament, filament_id)
    if not f:
        raise HTTPException(404, "Not found")
    for row in session.exec(select(FilamentPrice).where(FilamentPrice.filament_id == filament_id)).all():
        session.delete(row)
    session.delete(f)
    session.commit()
    return {"status": "deleted"}
