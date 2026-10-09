import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import Request
from sqlmodel import Session, select

from app.config import CONFIG_PATH
from app.settings_store import get_setting, set_setting

SESSION_COOKIE = "modelhub_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
SECRET_KEY_PATH = CONFIG_PATH / "secret.key"

# Paths reachable with no session at all -- health checks, the login/setup API
# itself, static assets needed to render the login page, and the browser
# extension's own upload endpoint (which authenticates via API key instead).
PUBLIC_PATHS = {"/api/health", "/api/auth/login", "/api/auth/setup", "/api/auth/status", "/manifest.webmanifest", "/sw.js", "/metrics"}
PUBLIC_PREFIXES = ("/assets/", "/share/", "/dl/", "/status/", "/quote/", "/hooks/", "/api/auth/oidc/")

# The extension API key is intentionally weaker than a full login session: it's
# stored in a browser extension, a lower-trust place than the server admin's own
# session cookie, so it must only ever unlock this one endpoint -- never settings,
# never account changes, never the rest of the library.
API_KEY_ALLOWED_PATHS = {"/api/library/import"}
# A slicer's "upload to OctoPrint / Moonraker" (Model Hub's virtual printer) sends an API token as X-Api-Key, on these paths only
VIRTUAL_PRINTER_PATHS = {"/api/version", "/api/files/local", "/server/info", "/printer/info", "/server/files/upload"}

# Settings never writable/readable through the generic /api/settings blob --
# they have their own dedicated, access-controlled endpoints instead. Without
# this, anyone holding only the (lower-trust) extension API key could read the
# password hash or overwrite it outright via a plain PUT to /api/settings.
RESERVED_SETTING_KEYS = {"auth_username", "auth_password_hash", "extension_api_key", "status_token", "shop_hook_token"}
# Secrets for single sign-on and shops: shown as dots once saved, never put in a backup
ACCESS_SECRETS = ("oidc_client_secret", "woo_key", "woo_secret", "shipstation_key", "shipstation_secret", "shop_hook_secret")


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


ROLES = ("member", "viewer", "printer")

# Only the administrator may use these at all (reading them included)...
ADMIN_ONLY_PREFIXES = ("/api/settings", "/api/backup", "/api/users", "/api/printers", "/api/tokens", "/api/spoolman", "/api/sensors")
# ...and these they alone may change (members can still look): they delete files from disk
ADMIN_ONLY_WRITE_PREFIXES = ("/api/duplicates",)
ADMIN_ONLY_PATHS = {"/api/library/non-model-files/remove", "/api/system/update-check", "/api/activity/export.csv"}
# Everyone signed in may manage their own sign-in (password and second step), whatever their role
SELF_SERVICE = ("/api/auth/me/password", "/api/auth/2fa/")
# Share links are secrets: viewers may not even list them
NO_VIEWER_PREFIXES = ("/api/shares",)


def current_user(request: Request, session: Session):
    """Who is making this request: {"username", "role"} with role admin / member / viewer, or
    "importer" for the browser extension's API key. None when not signed in. Roles are looked up
    on every request, so deleting a user or changing a role takes effect immediately."""
    token = request.cookies.get(SESSION_COOKIE)
    username = verify_session_token(token) if token else None
    if username is not None:
        if username == get_setting(session, "auth_username"):
            return {"username": username, "role": "admin"}
        from app.models import AppUser
        row = session.exec(select(AppUser).where(AppUser.username == username)).first()
        if row:
            return {"username": row.username, "role": row.role if row.role in ROLES else "viewer"}
    if request.url.path in API_KEY_ALLOWED_PATHS:
        api_key = request.headers.get("x-model-hub-api-key")
        stored_key = get_setting(session, "extension_api_key")
        if api_key and stored_key and hmac.compare_digest(api_key, stored_key):
            return {"username": "extension", "role": "importer"}
    if request.url.path in VIRTUAL_PRINTER_PATHS and request.headers.get("x-api-key"):
        from app import tokens                        # a slicer sends the token as an OctoPrint/Moonraker API key
        row = tokens.lookup(session, request.headers["x-api-key"].strip())
        if row and tokens.SCOPE_ROLES.get(row.scope) in ("member", "viewer"):
            return {"username": f"token:{row.name}", "role": tokens.SCOPE_ROLES[row.scope]}
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        from app import tokens
        row = tokens.lookup(session, bearer[7:].strip())
        if row:
            role = tokens.SCOPE_ROLES.get(row.scope)
            if role == "importer" and request.url.path not in API_KEY_ALLOWED_PATHS:
                return None
            if role:
                return {"username": f"token:{row.name}", "role": role}
    return None


def request_is_authenticated(request: Request, session: Session) -> bool:
    return current_user(request, session) is not None


PRINTER_ROLE_DENIED_READS = ("/api/settings", "/api/backup", "/api/users", "/api/tokens", "/api/spoolman", "/api/sensors", "/api/shares")
START_PRINT = re.compile(r"^/api/(queue/\d+/send|printers/\d+/(control|send|plate-cleared)|printers/bulk-control)$")


def printer_role_reason(method: str, path: str):
    """A login that may look at everything (but not the administrator's pages) and start, pause, resume and cancel prints, and nothing else."""
    if path.startswith(SELF_SERVICE):
        return None
    if method in ("GET", "HEAD"):
        return "Your account is not allowed to see that." if path.startswith(PRINTER_ROLE_DENIED_READS) else None
    if method == "POST" and START_PRINT.match(path):
        return None
    return "Your account can start and stop prints but cannot change anything else."


def forbidden_reason(user: dict, method: str, path: str):
    """Why this signed-in user may not do this, or None if they may."""
    role = user["role"]
    if role in ("admin", "importer") or path == "/api/auth/logout":
        return None
    if role == "printer":
        return printer_role_reason(method, path)
    writing = method not in ("GET", "HEAD")
    sends_to_printer = (path.startswith("/api/queue/") and path.endswith("/send")      # starting a job on a real printer
                        or (path.startswith("/api/prints/") and path.endswith("/timelapse-candidates")))      # asks a printer, and shows where it is
    if (path.startswith(ADMIN_ONLY_PREFIXES) or path in ADMIN_ONLY_PATHS or sends_to_printer
            or (writing and path.startswith(ADMIN_ONLY_WRITE_PREFIXES))):
        return "Only the administrator can do that."
    if role == "viewer" and path.startswith(NO_VIEWER_PREFIXES):
        return "Your account is read-only."
    if role == "viewer" and writing and not path.startswith(SELF_SERVICE):
        return "Your account is read-only."
    return None


def path_requires_auth(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return False
    if any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return False
    if path == "/":
        return False  # index.html itself does an auth check client-side and redirects to /login
    return True
