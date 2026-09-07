import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from fastapi import Request
from sqlmodel import Session

from app.config import CONFIG_PATH
from app.settings_store import get_setting, set_setting

SESSION_COOKIE = "modelhub_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
SECRET_KEY_PATH = CONFIG_PATH / "secret.key"

# Paths reachable with no session at all -- health checks, the login/setup API
# itself, static assets needed to render the login page, and the browser
# extension's own upload endpoint (which authenticates via API key instead).
PUBLIC_PATHS = {"/api/health", "/api/auth/login", "/api/auth/setup", "/api/auth/status"}
PUBLIC_PREFIXES = ("/assets/",)

# The extension API key is intentionally weaker than a full login session: it's
# stored in a browser extension, a lower-trust place than the server admin's own
# session cookie, so it must only ever unlock this one endpoint -- never settings,
# never account changes, never the rest of the library.
API_KEY_ALLOWED_PATHS = {"/api/library/import"}

# Settings never writable/readable through the generic /api/settings blob --
# they have their own dedicated, access-controlled endpoints instead. Without
# this, anyone holding only the (lower-trust) extension API key could read the
# password hash or overwrite it outright via a plain PUT to /api/settings.
RESERVED_SETTING_KEYS = {"auth_username", "auth_password_hash", "extension_api_key"}


def _secret_key() -> bytes:
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_bytes()
    key = secrets.token_bytes(32)
    SECRET_KEY_PATH.write_bytes(key)
    return key


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(expected.hex(), digest_hex)


def make_session_token(username: str) -> str:
    expiry = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{username}:{expiry}"
    sig = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_session_token(token: str) -> str | None:
    try:
        username, expiry, sig = token.rsplit(":", 2)
    except ValueError:
        return None
    payload = f"{username}:{expiry}"
    expected = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    if int(expiry) < time.time():
        return None
    return username


LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60

# In-memory only -- fine for a single-container app with no shared state across
# instances. Keyed by client IP so one attacker can't lock out the real admin,
# and cleared on a successful login so a legitimate user who fumbles a few times
# isn't stuck waiting out the window.
_login_attempts: dict[str, list[float]] = {}


def check_login_rate_limit(key: str) -> int | None:
    """Returns seconds to wait if the key is currently rate-limited, else None."""
    now = time.time()
    attempts = [t for t in _login_attempts.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[key] = attempts
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        return max(1, int(LOGIN_WINDOW_SECONDS - (now - attempts[0])))
    return None


def record_failed_login(key: str):
    _login_attempts.setdefault(key, []).append(time.time())


def clear_login_attempts(key: str):
    _login_attempts.pop(key, None)


def is_configured(session: Session) -> bool:
    return get_setting(session, "auth_password_hash") is not None


def bootstrap_from_env(session: Session):
    """If AUTH_USERNAME/AUTH_PASSWORD env vars are set and no account exists yet,
    create it automatically -- lets an Unraid template set credentials at deploy time
    without a manual setup step."""
    if is_configured(session):
        return
    env_user = os.environ.get("AUTH_USERNAME")
    env_pass = os.environ.get("AUTH_PASSWORD")
    if env_user and env_pass:
        set_setting(session, "auth_username", env_user)
        set_setting(session, "auth_password_hash", hash_password(env_pass))


def ensure_extension_api_key(session: Session) -> str:
    key = get_setting(session, "extension_api_key")
    if not key:
        key = secrets.token_urlsafe(24)
        set_setting(session, "extension_api_key", key)
    return key


def request_is_authenticated(request: Request, session: Session) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if token and verify_session_token(token):
        return True
    if request.url.path in API_KEY_ALLOWED_PATHS:
        api_key = request.headers.get("x-model-hub-api-key")
        stored_key = get_setting(session, "extension_api_key")
        if api_key and stored_key and hmac.compare_digest(api_key, stored_key):
            return True
    return False


def path_requires_auth(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return False
    if any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return False
    if path == "/":
        return False  # index.html itself does an auth check client-side and redirects to /login
    return True
