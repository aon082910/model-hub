from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
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


@router.get("/logs")
def get_logs(level: str = "INFO", q: Optional[str] = None, limit: int = Query(200, ge=1, le=1000)):
    """The latest log lines (administrator only), newest last, with secrets blanked."""
    from app import logbuffer
    return {"lines": logbuffer.lines(level, q, limit)}


@router.get("/support-bundle.zip")
def support_bundle(session: Session = Depends(get_session)):
    """A zip for asking for help: version, what is stored (counts), which settings are set (names only), printers without keys, and the recent log."""
    from app import discovery
    return Response(discovery.support_bundle(session), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="modelhub-support.zip"'})


@router.post("/update-check")
def check_now(session: Session = Depends(get_session)):
    """Ask GitHub now (administrator only: see auth.ADMIN_ONLY_PATHS)."""
    try:
        return update_check.check(session, force=True)
    except update_check.UpdateCheckError as e:
        raise HTTPException(502, str(e))
