import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlmodel import Session

from app import metrics
from app.db import get_session
from app.settings_store import get_setting

router = APIRouter(tags=["metrics"], include_in_schema=False)


@router.get("/metrics")
def scrape(request: Request, session: Session = Depends(get_session)):
    """What Prometheus reads. Off (404) until switched on in Settings; if a token is set it must come as `Authorization: Bearer <token>`."""
    if not metrics.enabled(session):
        raise HTTPException(404, "Not found")
    token = (get_setting(session, "metrics_token", "") or "").strip()
    if token:
        given = request.headers.get("authorization", "")
        if not (given.lower().startswith("bearer ") and hmac.compare_digest(given[7:].strip().encode(), token.encode())):
            raise HTTPException(401, "A bearer token is needed", headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(metrics.render(session), media_type="text/plain; version=0.0.4; charset=utf-8", headers={"Cache-Control": "no-store"})
