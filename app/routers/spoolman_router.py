from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from app import activity, spoolman
from app.db import get_session

router = APIRouter(prefix="/api/spoolman", tags=["spoolman"])


def _wrap(call, *args):
    try:
        return call(*args)
    except spoolman.SpoolmanError as e:
        raise HTTPException(502, str(e))


@router.post("/test")
def test(session: Session = Depends(get_session)):
    return {"status": "ok", "message": _wrap(spoolman.test, session)}


@router.post("/import")
def import_spools(request: Request, session: Session = Depends(get_session)):
    result = _wrap(spoolman.import_spools, session)
    activity.record(session, activity.actor_of(request), "spoolman", f"Imported from Spoolman: {result['added']} new, {result['updated']} updated")
    return result


@router.post("/export")
def export_spools(request: Request, session: Session = Depends(get_session)):
    result = _wrap(spoolman.export_spools, session)
    activity.record(session, activity.actor_of(request), "spoolman", f"Sent {result['created']} spool(s) to Spoolman")
    return result
