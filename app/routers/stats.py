from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app import stats
from app.db import get_session

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
def get_stats(months: int = Query(12, ge=1, le=60), session: Session = Depends(get_session)):
    return stats.build(session, months)
