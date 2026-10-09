from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session

from app import quotes
from app.db import get_session

public_router = APIRouter(tags=["quotes"], include_in_schema=False)

HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"}


def _order(session: Session, request: Request, given: str):
    client = request.client.host if request.client else "unknown"
    if quotes.too_many_misses(client):
        raise HTTPException(429, "Too many tries. Wait a few minutes")
    order = quotes.find(session, given)
    if not order:
        quotes.note_miss(client)
        raise HTTPException(404, "Not found")
    return order


@public_router.get("/quote/{given}")
def page(given: str, request: Request, session: Session = Depends(get_session)):
    _order(session, request, given)
    return HTMLResponse(quotes.PAGE, headers=HEADERS)


@public_router.get("/quote/{given}/data")
def data(given: str, request: Request, session: Session = Depends(get_session)):
    return JSONResponse(quotes.snapshot(session, _order(session, request, given)), headers=HEADERS)


@public_router.post("/quote/{given}/respond")
def respond(given: str, payload: dict, request: Request, session: Session = Depends(get_session)):
    order = _order(session, request, given)
    if not isinstance(payload.get("accept"), bool):
        raise HTTPException(400, "accept must be true or false")
    name, message = payload.get("name") or "", payload.get("message") or ""
    if not isinstance(name, str) or not isinstance(message, str):
        raise HTTPException(400, "The name and message must be text")
    try:
        status = quotes.answer(session, order, payload["accept"], name, message)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"status": status}, headers=HEADERS)
