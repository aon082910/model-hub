from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app import source_updates, sources
from app.db import get_session

router = APIRouter(prefix="/api/source-updates", tags=["source-updates"])


def _listing(payload: dict):
    provider, source_id = payload.get("provider"), str(payload.get("source_id") or "")
    if provider not in sources.PROVIDERS or not sources.valid_source_id(provider, source_id):
        raise HTTPException(400, "Give a provider and the listing's id")
    return provider, source_id


@router.get("/status")
def status():
    return source_updates.job_status()


@router.post("/start")
def start(payload: dict):
    try:
        return source_updates.start_job(force=payload.get("force") is True)
    except source_updates.JobBusy as e:
        raise HTTPException(409, str(e))


@router.post("/stop")
def stop():
    return source_updates.stop_job()


@router.get("/changed")
def changed(session: Session = Depends(get_session)):
    return {"listings": source_updates.changed_listings(session)}


@router.post("/check")
def check_one(payload: dict, session: Session = Depends(get_session)):
    provider, source_id = _listing(payload)
    try:
        outcome = source_updates.check_listing(session, provider, source_id, sources.load_credentials(session))
    except sources.SourceError as e:
        raise HTTPException(502, str(e))
    return {"outcome": outcome}


@router.post("/dismiss")
def dismiss(payload: dict, session: Session = Depends(get_session)):
    """Accept the listing as it is now: the flag goes away and later changes are measured from here."""
    provider, source_id = _listing(payload)
    try:
        source_updates.check_listing(session, provider, source_id, sources.load_credentials(session), accept=True)
    except sources.SourceError as e:
        raise HTTPException(502, str(e))
    return {"status": "dismissed"}


@router.post("/refresh")
def refresh(payload: dict, session: Session = Depends(get_session)):
    """Pull the listing's current title, description, tags and pictures into the linked models."""
    provider, source_id = _listing(payload)
    try:
        return {"models": source_updates.refresh_listing(session, provider, source_id)}
    except sources.SourceError as e:
        raise HTTPException(502, str(e))
