from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session

from app import status_page
from app.db import get_session

router = APIRouter(prefix="/api/settings/status-page", tags=["status-page"])          # administrator only (see app.auth)
public_router = APIRouter(tags=["status-page"], include_in_schema=False)

HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"}


@router.get("")
def get_status_page(session: Session = Depends(get_session)):
    return status_page.settings(session)


@router.post("")
def set_status_page(payload: dict, session: Session = Depends(get_session)):
    """enabled: true/false, show_files: true/false, rotate: true makes a new secret link (the old one stops working)."""
    for key in ("enabled", "show_files", "rotate"):
        if key in payload and not isinstance(payload[key], bool):
            raise HTTPException(400, f"{key} must be true or false")
    return status_page.configure(session, payload.get("enabled"), payload.get("show_files"), payload.get("rotate") is True)


@public_router.get("/status/{given}")
def page(given: str, session: Session = Depends(get_session)):
    if not status_page.valid(session, given):
        raise HTTPException(404, "Not found")
    return HTMLResponse(status_page.PAGE, headers=HEADERS)


@public_router.get("/status/{given}/data")
def data(given: str, session: Session = Depends(get_session)):
    if not status_page.valid(session, given):
        raise HTTPException(404, "Not found")
    return JSONResponse(status_page.snapshot(session), headers=HEADERS)
