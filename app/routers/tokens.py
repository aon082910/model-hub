"""The administrator's API tokens (see app.tokens)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app import tokens
from app.db import get_session
from app.models import ApiToken

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

MAX_TOKENS = 50


def _json(row: ApiToken) -> dict:
    return {"id": row.id, "name": row.name, "prefix": row.prefix, "scope": row.scope, "created_by": row.created_by,
            "created_at": row.created_at, "last_used_at": row.last_used_at, "expires_at": row.expires_at}


@router.get("")
def list_tokens(session: Session = Depends(get_session)):
    return {"tokens": [_json(t) for t in session.exec(select(ApiToken).order_by(ApiToken.created_at.desc())).all()],
            "scopes": list(tokens.SCOPES)}


@router.post("")
def create_token(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Make a token. The key is in the answer once and never again."""
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "Give the token a name (what it is for)")
    scope = payload.get("scope", "read")
    if scope not in tokens.SCOPES:
        raise HTTPException(400, "scope must be one of: " + ", ".join(tokens.SCOPES))
    days = payload.get("expires_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, (int, float)) or not (0 < days <= 3650)):
        raise HTTPException(400, "expires_days must be between 1 and 3650")
    if len(session.exec(select(ApiToken.id)).all()) >= MAX_TOKENS:
        raise HTTPException(400, f"At most {MAX_TOKENS} tokens")
    user = getattr(request.state, "user", None) or {}
    row, key = tokens.create(session, name.strip()[:80], scope, user.get("username"), days)
    return {**_json(row), "token": key}


@router.delete("/{token_id}")
def revoke_token(token_id: int, session: Session = Depends(get_session)):
    row = session.get(ApiToken, token_id)
    if not row:
        raise HTTPException(404, "Not found")
    session.delete(row)
    session.commit()
    return {"status": "revoked"}
