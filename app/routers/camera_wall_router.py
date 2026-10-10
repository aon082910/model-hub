from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlmodel import Session

from app import camera_wall
from app.db import get_session
from app.models import Printer

router = APIRouter(prefix="/api/settings/camera-wall", tags=["camera-wall"])          # administrator only (see app.auth)
public_router = APIRouter(tags=["camera-wall"], include_in_schema=False)

HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self'; connect-src 'self'"}


@router.get("")
def get_wall(session: Session = Depends(get_session)):
    return camera_wall.settings(session)


@router.post("")
def set_wall(payload: dict, session: Session = Depends(get_session)):
    """enabled: true/false, rotate: true makes a new secret link (the old one stops working)."""
    for key in ("enabled", "rotate"):
        if key in payload and not isinstance(payload[key], bool):
            raise HTTPException(400, f"{key} must be true or false")
    return camera_wall.configure(session, payload.get("enabled"), payload.get("rotate") is True)


def _check(session: Session, given: str) -> None:
    if not camera_wall.valid(session, given):
        raise HTTPException(404, "Not found")


@public_router.get("/wall/{given}")
def page(given: str, session: Session = Depends(get_session)):
    _check(session, given)
    return HTMLResponse(camera_wall.PAGE, headers=HEADERS)


@public_router.get("/wall/{given}/data")
def data(given: str, session: Session = Depends(get_session)):
    _check(session, given)
    return JSONResponse(camera_wall.snapshot(session), headers=HEADERS)


@public_router.get("/wall/{given}/cam/{printer_id}.jpg")
def cam(given: str, printer_id: int, session: Session = Depends(get_session)):
    _check(session, given)
    printer = session.get(Printer, printer_id)
    if not printer or not printer.snapshot_url:
        raise HTTPException(404, "Not found")
    picture = camera_wall.picture(session, printer)
    if not picture:
        raise HTTPException(502, "The camera did not answer")
    return Response(picture, media_type="image/jpeg", headers=HEADERS)
