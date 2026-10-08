from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from app import update_check, version
from app.db import get_session

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/version")
def get_version(request: Request, session: Session = Depends(get_session)):
    """The running release; the administrator also sees whether a newer one is known."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") == "admin":
        return update_check.status(session)
    return {"current": version.VERSION}


@router.post("/update-check")
def check_now(session: Session = Depends(get_session)):
    """Ask GitHub now (administrator only: see auth.ADMIN_ONLY_PATHS)."""
    try:
        return update_check.check(session, force=True)
    except update_check.UpdateCheckError as e:
        raise HTTPException(502, str(e))
