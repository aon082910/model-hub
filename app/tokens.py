"""Personal API tokens: create, recognise, and keep track of use."""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models import ApiToken

PREFIX = "mh_"
SCOPES = ("read", "write", "import")
# what each scope may do: read = look at everything a viewer can, write = what a member can, import = only add models
SCOPE_ROLES = {"read": "viewer", "write": "member", "import": "importer"}
TOUCH_EVERY = timedelta(minutes=1)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(session: Session, name: str, scope: str, created_by: Optional[str], expires_days: Optional[float] = None) -> tuple:
    """Returns (row, plaintext token). The plaintext exists only in this return value."""
    token = PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(name=name, token_hash=hash_token(token), prefix=token[:10], scope=scope, created_by=created_by,
                   expires_at=datetime.utcnow() + timedelta(days=expires_days) if expires_days else None)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, token


def lookup(session: Session, token: str) -> Optional[ApiToken]:
    """The live token row for this plaintext token, or None (unknown or expired). Notes the use."""
    if not token.startswith(PREFIX) or len(token) > 128:
        return None
    row = session.exec(select(ApiToken).where(ApiToken.token_hash == hash_token(token))).first()
    if not row or (row.expires_at and row.expires_at < datetime.utcnow()):
        return None
    now = datetime.utcnow()
    if not row.last_used_at or now - row.last_used_at > TOUCH_EVERY:
        row.last_used_at = now
        session.add(row)
        session.commit()
    return row
