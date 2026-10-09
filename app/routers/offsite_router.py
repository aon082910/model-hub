from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from app import activity, offsite
from app.db import get_session

router = APIRouter(prefix="/api/backup/offsite", tags=["offsite"])


@router.post("/test")
def test(session: Session = Depends(get_session)):
    try:
        return {"status": "ok", "where": offsite.test(session)}
    except offsite.OffsiteError as e:
        raise HTTPException(502, str(e))


@router.post("/now")
def now(request: Request, session: Session = Depends(get_session)):
    """Send the newest saved backup now (making one first if there is none)."""
    from app import backup
    try:
        if not offsite.newest_backup():
            backup.save_backup("modelhub-backup")
        where = offsite.run(session, force=True)
    except offsite.OffsiteError as e:
        raise HTTPException(502, str(e))
    if where is None:
        raise HTTPException(400, "Choose where to send the backups first")
    activity.record(session, activity.actor_of(request), "backup", "Sent a backup off-site")
    return {"status": "sent", "where": where}
