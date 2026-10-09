from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app import signin

from app.db import get_session
from app.settings_store import get_setting, set_setting
from app.auth import (
    is_configured, hash_password, verify_password, make_session_token,
    SESSION_COOKIE, SESSION_TTL_SECONDS, ensure_extension_api_key,
    check_login_rate_limit, record_failed_login, clear_login_attempts,
)
from app.models import AppUser

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
def status(session: Session = Depends(get_session)):
    return {"configured": is_configured(session), "sso": signin.oidc_enabled(session), "sso_name": (get_setting(session, "oidc_name", "") or "Single sign-on")[:40]}


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


def _start_session(request: Request, response, username: str) -> None:
    token = make_session_token(username)
    # behind an HTTPS reverse proxy the cookie must travel only over HTTPS
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="lax", secure=secure)


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
    if not isinstance(username, str) or not isinstance(password, str):
        raise HTTPException(400, "Username and password must be text")
    stored_user = get_setting(session, "auth_username")
    stored_hash = get_setting(session, "auth_password_hash")
    is_admin = bool(stored_user and stored_hash and username == stored_user and verify_password(password, stored_hash))
    if not is_admin:
        member = session.exec(select(AppUser).where(AppUser.username == username)).first() if username else None
        local = bool(member and (member.source or "local") == "local")
        # verify against *something* either way, so a wrong user name takes as long as a wrong password
        proved = bool(local and verify_password(password, member.password_hash))
        if not proved and not local and username != stored_user:
            try:
                proved = signin.ldap_authenticate(session, username, password) is not None     # a directory may know this person
            except signin.SignInError as e:
                signin.audit(session, username, f"LDAP sign-in problem: {e}")
        if not proved:
            if not member:
                verify_password(password, hash_password("not a real password"))
            record_failed_login(client_ip)
            signin.audit(session, username or "unknown", f"Failed sign-in as {signin._clean(username)} from {client_ip}")
            raise HTTPException(401, "Invalid username or password")
    if signin.totp_enabled(session, username):
        code = payload.get("code")
        if not code:
            return JSONResponse(status_code=401, content={"detail": "Enter the code from your authenticator app (or a backup code)", "totp_required": True})
        if not isinstance(code, str) or not signin.check_second_step(session, username, code):
            record_failed_login(client_ip)
            signin.audit(session, username, f"Wrong second-step code for {signin._clean(username)} from {client_ip}")
            return JSONResponse(status_code=401, content={"detail": "That code is not right", "totp_required": True})
    clear_login_attempts(client_ip)
    _start_session(request, response, username)
    signin.audit(session, username, f"Signed in from {client_ip}")
    return {"status": "ok"}


@router.get("/oidc/start")
def oidc_start(request: Request, session: Session = Depends(get_session)):
    """Send the browser to the single sign-on provider."""
    if not signin.oidc_enabled(session):
        raise HTTPException(404, "Single sign-on is not set up")
    client_ip = request.client.host if request.client else "unknown"
    retry_after = check_login_rate_limit(client_ip)
    if retry_after is not None:
        raise HTTPException(429, f"Too many login attempts. Try again in {retry_after} seconds.", headers={"Retry-After": str(retry_after)})
    try:
        return RedirectResponse(signin.start(session, signin.redirect_uri(session, request)), status_code=302)
    except signin.SignInError as e:
        raise HTTPException(502, str(e))


@router.get("/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "", session: Session = Depends(get_session)):
    client_ip = request.client.host if request.client else "unknown"
    retry_after = check_login_rate_limit(client_ip)
    if retry_after is not None:
        raise HTTPException(429, f"Too many login attempts. Try again in {retry_after} seconds.", headers={"Retry-After": str(retry_after)})
    try:
        if error:
            raise signin.SignInError("The provider turned the sign-in down")
        user = signin.finish(session, code, state, signin.redirect_uri(session, request))
    except signin.SignInError as e:
        record_failed_login(client_ip)
        signin.audit(session, "unknown", f"Failed single sign-on from {client_ip}: {e}")
        from urllib.parse import quote
        return RedirectResponse("/?sso_error=" + quote(str(e)[:200]), status_code=302)
    clear_login_attempts(client_ip)
    response = RedirectResponse("/", status_code=302)
    _start_session(request, response, user.username)
    signin.audit(session, user.username, f"Signed in with single sign-on from {client_ip}")
    return response


@router.get("/me")
def me(request: Request, session: Session = Depends(get_session)):
    """Who is signed in and what they may do (the page uses this to hide what is not theirs)."""
    user = getattr(request.state, "user", None) or {}
    named = user.get("username") or ""
    row = session.exec(select(AppUser).where(AppUser.username == named)).first() if named else None
    return {"username": user.get("username"), "role": user.get("role"), "totp": signin.totp_enabled(session, named) if named else False,
            "source": (row.source or "local") if row else "local", "can_two_step": bool(named) and not named.startswith("token:") and named != "extension"}


def _own_name(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    name = user.get("username") or ""
    if not name or name.startswith("token:") or name == "extension":
        raise HTTPException(400, "Sign in with your account to do that")
    return name


@router.post("/2fa/begin")
def two_step_begin(request: Request, session: Session = Depends(get_session)):
    """Start adding an authenticator app: a secret (and the otpauth:// address behind a QR code) to put in the app."""
    try:
        begun = signin.begin_enrolment(session, _own_name(request))
    except signin.SignInError as e:
        raise HTTPException(409, str(e))
    try:
        import segno
        begun["qr"] = segno.make(begun["uri"], error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
    except Exception:
        begun["qr"] = None                          # the secret can still be typed into the app
    return begun


@router.post("/2fa/enable")
def two_step_enable(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Prove the app works with its first code: two-step sign-in is then on, and the single-use backup codes are shown (once)."""
    name = _own_name(request)
    try:
        codes = signin.confirm_enrolment(session, name, str(payload.get("code") or ""))
    except signin.SignInError as e:
        raise HTTPException(400, str(e))
    signin.audit(session, name, "Two-step sign-in switched on")
    return {"backup_codes": codes}


@router.post("/2fa/disable")
def two_step_disable(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Switch it off. It needs the account's password and a current code (or a backup code), so a left-open browser cannot do it."""
    name = _own_name(request)
    user = getattr(request.state, "user", None) or {}
    password = payload.get("password") or ""
    if user.get("role") == "admin":
        ok = verify_password(password, get_setting(session, "auth_password_hash") or "")
    else:
        row = session.exec(select(AppUser).where(AppUser.username == name)).first()
        ok = bool(row and ((row.source or "local") != "local" or verify_password(password, row.password_hash)))
    if not ok or not signin.check_second_step(session, name, str(payload.get("code") or "")):
        raise HTTPException(401, "The password or the code is not right")
    signin.disable_totp(session, name)
    signin.audit(session, name, "Two-step sign-in switched off")
    return {"status": "ok"}


@router.post("/me/password")
def change_own_password(payload: dict, request: Request, session: Session = Depends(get_session)):
    """A member's or viewer's own password (the administrator uses /change-password)."""
    user = getattr(request.state, "user", None) or {}
    if user.get("role") not in ("member", "viewer", "printer"):
        raise HTTPException(400, "The administrator changes their password with the Account section")
    row = session.exec(select(AppUser).where(AppUser.username == user["username"])).first()
    if not row or not verify_password(payload.get("current_password") or "", row.password_hash):
        raise HTTPException(401, "Current password is incorrect")
    new_password = payload.get("new_password") or ""
    if len(new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    row.password_hash = hash_password(new_password)
    session.add(row)
    session.commit()
    return {"status": "ok"}


@router.post("/logout")
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    user = getattr(request.state, "user", None) or {}
    if user.get("username"):
        signin.audit(session, user["username"], "Signed out")
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
