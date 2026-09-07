from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session

from app.db import get_session
from app.settings_store import get_setting, set_setting
from app.auth import (
    is_configured, hash_password, verify_password, make_session_token,
    SESSION_COOKIE, SESSION_TTL_SECONDS, ensure_extension_api_key,
    check_login_rate_limit, record_failed_login, clear_login_attempts,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
def status(session: Session = Depends(get_session)):
    return {"configured": is_configured(session)}


@router.post("/setup")
def setup(payload: dict, session: Session = Depends(get_session)):
    """First-run only: creates the single admin account. Refuses once one exists --
    use /api/auth/login (and change the password from Settings) after that."""
    if is_configured(session):
        raise HTTPException(409, "Already configured")
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(400, "Username required, password must be at least 8 characters")
    set_setting(session, "auth_username", username)
    set_setting(session, "auth_password_hash", hash_password(password))
    ensure_extension_api_key(session)
    return {"status": "ok"}


@router.post("/login")
def login(payload: dict, request: Request, response: Response, session: Session = Depends(get_session)):
    client_ip = request.client.host if request.client else "unknown"
    retry_after = check_login_rate_limit(client_ip)
    if retry_after is not None:
        raise HTTPException(
            429, f"Too many login attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
    username = payload.get("username") or ""
    password = payload.get("password") or ""
    stored_user = get_setting(session, "auth_username")
    stored_hash = get_setting(session, "auth_password_hash")
    if not stored_user or not stored_hash or username != stored_user or not verify_password(password, stored_hash):
        record_failed_login(client_ip)
        raise HTTPException(401, "Invalid username or password")
    clear_login_attempts(client_ip)
    token = make_session_token(username)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS,
        httponly=True, samesite="lax",
    )
    return {"status": "ok"}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@router.post("/change-password")
def change_password(payload: dict, session: Session = Depends(get_session)):
    stored_hash = get_setting(session, "auth_password_hash")
    if not stored_hash or not verify_password(payload.get("current_password") or "", stored_hash):
        raise HTTPException(401, "Current password is incorrect")
    new_password = payload.get("new_password") or ""
    if len(new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    set_setting(session, "auth_password_hash", hash_password(new_password))
    return {"status": "ok"}
