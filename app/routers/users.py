"""The administrator's list of other logins."""
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import ROLES, hash_password
from app.db import get_session
from app.models import AppUser
from app.settings_store import get_setting

router = APIRouter(prefix="/api/users", tags=["users"])

MAX_USERS = 50
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,39}")


def _json(user: AppUser) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role, "created_at": user.created_at}


def _role(value) -> str:
    if value not in ROLES:
        raise HTTPException(400, "role must be one of: " + ", ".join(ROLES))
    return value


def _password(value) -> str:
    if not isinstance(value, str) or len(value) < 8:
        raise HTTPException(400, "The password must be at least 8 characters")
    return value


@router.get("")
def list_users(session: Session = Depends(get_session)):
    admin = get_setting(session, "auth_username")
    users = session.exec(select(AppUser).order_by(AppUser.username)).all()
    return {"admin": admin, "users": [_json(u) for u in users], "roles": list(ROLES)}


@router.post("")
def create_user(payload: dict, session: Session = Depends(get_session)):
    username = payload.get("username")
    if not isinstance(username, str) or not _NAME.fullmatch(username.strip()):
        raise HTTPException(400, "The user name must be 1-40 letters, numbers or . _ @ - (starting with a letter or number)")
    username = username.strip()
    admin = (get_setting(session, "auth_username") or "").lower()
    taken = {u.lower() for u in session.exec(select(AppUser.username)).all()} | {admin}
    if username.lower() in taken:
        raise HTTPException(409, "That user name is taken")
    if len(session.exec(select(AppUser.id)).all()) >= MAX_USERS:
        raise HTTPException(400, f"At most {MAX_USERS} extra users")
    user = AppUser(username=username, password_hash=hash_password(_password(payload.get("password"))),
                   role=_role(payload.get("role", "member")))
    session.add(user)
    session.commit()
    session.refresh(user)
    return _json(user)


@router.patch("/{user_id}")
def update_user(user_id: int, payload: dict, session: Session = Depends(get_session)):
    user = session.get(AppUser, user_id)
    if not user:
        raise HTTPException(404, "Not found")
    if "role" in payload:
        user.role = _role(payload["role"])
    if "password" in payload:
        user.password_hash = hash_password(_password(payload["password"]))
    session.add(user)
    session.commit()
    session.refresh(user)
    return _json(user)


@router.delete("/{user_id}")
def delete_user(user_id: int, session: Session = Depends(get_session)):
    user = session.get(AppUser, user_id)
    if not user:
        raise HTTPException(404, "Not found")
    session.delete(user)
    session.commit()
    return {"status": "deleted"}
