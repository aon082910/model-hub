from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app import analytics
from app.db import get_session

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _day(value: Optional[str], name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{name} must be a date like 2026-10-31")


@router.get("")
def report(start: Optional[str] = None, end: Optional[str] = None, printer_id: Optional[int] = None, model_id: Optional[int] = None,
           material: Optional[str] = None, session: Session = Depends(get_session)):
    """Print jobs, success rate, time, filament and cost for a stretch of time (the last 30 days unless said), filtered by printer, model or material."""
    first, last = _day(start, "start"), _day(end, "end")
    if first and last and first > last:
        raise HTTPException(400, "start must not be after end")
    if first and last and (last - first).days > 3660:
        raise HTTPException(400, "At most ten years at a time")
    return analytics.build(session, first, last, printer_id, model_id, material)
