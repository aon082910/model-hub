from fastapi import APIRouter, Depends
from sqlmodel import Session

from app import storage
from app.db import get_session

router = APIRouter(prefix="/api/storage", tags=["storage"])


@router.get("")
def storage_overview(session: Session = Depends(get_session)):
    return storage.build(session)
