"""Signing in beyond a password: a second step (authenticator-app codes), single sign-on (OpenID Connect) and a directory (LDAP), plus a record of sign-in events.

* **Two-step sign-in (TOTP).** Any account can add an authenticator app (RFC 6238: 6 digits, 30 seconds). Once it is switched on, a correct password is not enough: the code is needed too.
  Eight single-use backup codes are shown once; only their hashes are kept. A code works once (a replay is refused).
* **Single sign-on (OpenID Connect).** The authorization-code flow with PKCE. Model Hub asks the provider's user-info endpoint who signed in (over the provider's own https address), so no
  token signature has to be checked here. A person who signs in this way becomes a *member* or *viewer* account (never the administrator), and only if you allow new accounts to be made
  (optionally only for some e-mail domains) or an account of that name already exists.
* **LDAP.** A password that is not a local one is tried as a directory bind (`ldap3`, loaded only when used) with a DN pattern you give. The same rules: never the administrator, a role you pick,
  and a local account of the same name is never taken over.
* Every sign-in, failed sign-in, sign-out and security change is written to the activity log (action *security*, visible to the administrator only), which can be exported as CSV.

Nothing here stores a password or a one-time code, and no secret is ever written into a backup."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
from sqlmodel import Session, select

from app import activity
from app.models import AppUser, UserSecurity
from app.settings_store import get_setting

STEP = 30
DIGITS = 6
WINDOW = 1                                   # a code from one step before or after is accepted (clocks differ a little)
BACKUP_CODES = 8
ISSUER = "Model Hub"
ROLES_FOR_OUTSIDE = ("member", "viewer")     # an account made by single sign-on or LDAP is never the administrator or the printer login


class SignInError(Exception):
    """A sign-in that cannot go on; the message is safe to show."""


# ---------------------------------------------------------------- the activity record
def _clean(text: str, limit: int = 60) -> str:
    return re.sub(r"[^\w@.\-+ ]", "?", str(text or ""))[:limit]


def audit(session: Session, actor: str, summary: str) -> None:
    activity.record(session, _clean(actor, 80) or "unknown", "security", summary[:500])


# ---------------------------------------------------------------- TOTP
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(number).zfill(DIGITS)


def code_at(secret: str, at: Optional[float] = None) -> str:
    return _code(secret, int((time.time() if at is None else at) // STEP))


def verify_code(secret: str, code: str, after_counter: int = 0, now: Optional[float] = None) -> Optional[int]:
    """The step number the code belongs to (so it can be remembered and not used twice), or None."""
    code = re.sub(r"\s", "", str(code or ""))
    if not re.fullmatch(r"\d{6}", code):
        return None
    now_counter = int((time.time() if now is None else now) // STEP)
    for counter in range(now_counter - WINDOW, now_counter + WINDOW + 1):
        if counter > after_counter and hmac.compare_digest(_code(secret, counter), code):
            return counter
    return None


def otpauth_uri(username: str, secret: str) -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(ISSUER)}:{quote(username)}?" + urlencode({"secret": secret, "issuer": ISSUER, "digits": DIGITS, "period": STEP})


def _hash_backup(code: str) -> str:
    return hashlib.sha256(re.sub(r"[\s-]", "", code).lower().encode()).hexdigest()


def new_backup_codes() -> list:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(BACKUP_CODES)]


def security_row(session: Session, username: str) -> Optional[UserSecurity]:
    return session.exec(select(UserSecurity).where(UserSecurity.username == username)).first()


def totp_enabled(session: Session, username: str) -> bool:
    row = security_row(session, username)
    return bool(row and row.totp_enabled and row.totp_secret)


def begin_enrolment(session: Session, username: str) -> dict:
    """A fresh secret to put in the authenticator app (not yet in force until a code from it is confirmed)."""
    row = security_row(session, username) or UserSecurity(username=username)
    if row.totp_enabled:
        raise SignInError("Two-step sign-in is already on; turn it off first to set it up again")
    row.totp_secret = new_secret()
    row.totp_enabled = False
    row.last_counter = 0
    session.add(row)
    session.commit()
    return {"secret": row.totp_secret, "uri": otpauth_uri(username, row.totp_secret)}


def confirm_enrolment(session: Session, username: str, code: str) -> list:
    """Switch it on once a code from the app proves the secret was copied right. Returns the backup codes (shown once)."""
    row = security_row(session, username)
    if not row or not row.totp_secret or row.totp_enabled:
        raise SignInError("Start the setup first")
    counter = verify_code(row.totp_secret, code)
    if counter is None:
        raise SignInError("That code is not right. Check the time on your phone and try the next code")
    codes = new_backup_codes()
    row.totp_enabled = True
    row.last_counter = counter
    row.backup_json = json.dumps([_hash_backup(c) for c in codes])
    session.add(row)
    session.commit()
    return codes


def check_second_step(session: Session, username: str, code: str) -> bool:
    """A code from the app (once only) or an unused backup code. A backup code is used up."""
    row = security_row(session, username)
    if not row or not row.totp_enabled:
        return True
    counter = verify_code(row.totp_secret, code, row.last_counter or 0)
    if counter is not None:
        row.last_counter = counter
        session.add(row)
        session.commit()
        return True
    try:
        backups = json.loads(row.backup_json or "[]")
    except ValueError:
        backups = []
    digest = _hash_backup(str(code or ""))
    if len(re.sub(r"[\s-]", "", str(code or ""))) == 8 and digest in backups:
        backups.remove(digest)
        row.backup_json = json.dumps(backups)
        session.add(row)
        session.commit()
        return True
    return False


def disable_totp(session: Session, username: str) -> bool:
    row = security_row(session, username)
    if not row:
        return False
    session.delete(row)
    session.commit()
    return True


def backup_codes_left(session: Session, username: str) -> int:
    row = security_row(session, username)
    try:
        return len(json.loads(row.backup_json or "[]")) if row else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------- outside accounts (single sign-on, LDAP)
def _s(session: Session, key: str) -> str:
    return (get_setting(session, key, "") or "").strip()


def outside_role(session: Session, key: str) -> str:
    role = _s(session, key)
    return role if role in ROLES_FOR_OUTSIDE else "viewer"


def account_for(session: Session, username: str, source: str, role: str, create: bool) -> AppUser:
    """The account of someone who proved who they are elsewhere. A local account of the same name is never used (that would let the
    directory or the provider take over a password account), and nothing is made unless creating is allowed."""
    row = session.exec(select(AppUser).where(AppUser.username == username)).first()
    if row:
        if (row.source or "local") != source:
            raise SignInError(f"A different account is already called {_clean(username)}")
        return row
    admin = _s(session, "auth_username")
    if username.lower() == admin.lower() or any(u.lower() == username.lower() for u in session.exec(select(AppUser.username)).all()):
        raise SignInError("That name belongs to the administrator account")
    if not create:
        raise SignInError("There is no account for you here yet. Ask the administrator to add you")
    row = AppUser(username=username[:120], password_hash="!" + secrets.token_hex(16), role=role, source=source)   # no password hash can match "!..."
    session.add(row)
    session.commit()
    session.refresh(row)
    audit(session, username, f"Account made on first {source} sign-in as {role}")
    return row


# ---------------------------------------------------------------- LDAP
LDAP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,39}$")          # the same shape as a local user name


def ldap_enabled(session: Session) -> bool:
    return bool(_s(session, "ldap_url") and "{username}" in _s(session, "ldap_dn"))


def _ldap_bind(url: str, dn: str, password: str) -> bool:
    """One bind with the person's own password (replaced in tests). ldap3 is imported only here."""
    try:
        import ssl
        from ldap3 import Connection, Server, Tls
    except ImportError:
        raise SignInError("LDAP sign-in needs the ldap3 package, which is not installed")
    tls = Tls(validate=ssl.CERT_REQUIRED) if url.lower().startswith("ldaps://") else None
    try:
        conn = Connection(Server(url, tls=tls, connect_timeout=8), user=dn, password=password, receive_timeout=10, auto_bind=True)
    except Exception:
        return False
    try:
        return bool(conn.bound)
    finally:
        try:
            conn.unbind()
        except Exception:
            pass


def ldap_authenticate(session: Session, username: str, password: str) -> Optional[AppUser]:
    """The account for a directory user whose password the directory accepts, else None."""
    if not ldap_enabled(session) or not password or not LDAP_NAME.match(username or ""):
        return None                                   # an empty password must never reach the directory (many treat it as an anonymous bind)
    url = _s(session, "ldap_url")
    if urlparse(url).scheme.lower() not in ("ldap", "ldaps") or not urlparse(url).hostname:
        return None
    try:
        from ldap3.utils.dn import escape_rdn
    except ImportError:
        escape_rdn = lambda text: re.sub(r'([,+"\\<>;=#])', r"\\\1", text)
    dn = _s(session, "ldap_dn").replace("{username}", escape_rdn(username))
    if not _ldap_bind(url, dn, password):
        return None
    return account_for(session, username, "ldap", outside_role(session, "ldap_role"), _s(session, "ldap_auto_create") != "false")


# ---------------------------------------------------------------- OpenID Connect
_states: dict = {}                      # state -> {verifier, expires}
_discovery: dict = {}                   # issuer -> (expires, document)
STATE_SECONDS = 600


def oidc_enabled(session: Session) -> bool:
    return bool(_s(session, "oidc_issuer") and _s(session, "oidc_client_id"))


def _https_or_plain(url: str) -> bool:
    p = urlparse(url or "")
    return p.scheme in ("http", "https") and bool(p.hostname) and not p.username and not p.password and not p.fragment


def discover(issuer: str) -> dict:
    cached = _discovery.get(issuer)
    if cached and cached[0] > time.time():
        return cached[1]
    if not _https_or_plain(issuer):
        raise SignInError("The single sign-on address is not a usable web address")
    try:
        r = httpx.get(issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=10, follow_redirects=False)
        doc = r.json()
    except Exception as e:
        raise SignInError(f"Could not read the provider's configuration ({e.__class__.__name__})")
    if r.status_code != 200 or not isinstance(doc, dict):
        raise SignInError("The provider did not return its configuration")
    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if not _https_or_plain(str(doc.get(key) or "")):
            raise SignInError(f"The provider's configuration has no usable {key.replace('_', ' ')}")
    _discovery[issuer] = (time.time() + 600, doc)
    return doc


def redirect_uri(session: Session, request) -> str:
    base = _s(session, "oidc_public_url")
    if not base:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
        host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host") or request.url.netloc
        base = f"{proto}://{host}"
    return base.rstrip("/") + "/api/auth/oidc/callback"


def start(session: Session, uri: str) -> str:
    """The address to send the browser to."""
    doc = discover(_s(session, "oidc_issuer"))
    now = time.time()
    for key in [k for k, v in _states.items() if v["expires"] < now]:
        _states.pop(key, None)
    if len(_states) > 500:
        raise SignInError("Too many sign-ins are waiting; try again in a few minutes")
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    _states[state] = {"verifier": verifier, "expires": now + STATE_SECONDS}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urlencode({"response_type": "code", "client_id": _s(session, "oidc_client_id"), "redirect_uri": uri, "scope": _s(session, "oidc_scopes") or "openid profile email",
                       "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return doc["authorization_endpoint"] + sep + query


def finish(session: Session, code: str, state: str, uri: str) -> AppUser:
    """Exchange the code, ask who signed in, and return that person's account (made if allowed)."""
    pending = _states.pop(state or "", None)
    if not pending or pending["expires"] < time.time():
        raise SignInError("That sign-in took too long or was not started here. Try again")
    if not code:
        raise SignInError("The provider did not send a code")
    doc = discover(_s(session, "oidc_issuer"))
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": uri, "client_id": _s(session, "oidc_client_id"), "code_verifier": pending["verifier"]}
    if _s(session, "oidc_client_secret"):
        data["client_secret"] = _s(session, "oidc_client_secret")
    try:
        token = httpx.post(doc["token_endpoint"], data=data, timeout=15, follow_redirects=False)
        access = token.json().get("access_token") if token.status_code == 200 else None
    except Exception as e:
        raise SignInError(f"The provider could not be reached ({e.__class__.__name__})")
    if not access:
        raise SignInError("The provider refused the sign-in")
    try:
        info = httpx.get(doc["userinfo_endpoint"], headers={"Authorization": f"Bearer {access}"}, timeout=15, follow_redirects=False).json()
    except Exception as e:
        raise SignInError(f"The provider did not say who you are ({e.__class__.__name__})")
    if not isinstance(info, dict) or not info.get("sub"):
        raise SignInError("The provider did not say who you are")
    email = str(info.get("email") or "")
    name = str(info.get("preferred_username") or "")
    if not name and email:
        if info.get("email_verified") is False:
            raise SignInError("The provider says your e-mail address is not verified")
        name = email
    name = name.strip()
    if not LDAP_NAME.match(name):
        raise SignInError("The provider gave no usable user name")
    domains = [d.strip().lower().lstrip("@") for d in re.split(r"[,\s;]+", _s(session, "oidc_allowed_domains")) if d.strip()]
    if domains and (info.get("email_verified") is False or email.rpartition("@")[2].lower() not in domains):
        raise SignInError("Your e-mail address is not from a domain that may sign in here")
    return account_for(session, name, "oidc", outside_role(session, "oidc_role"), _s(session, "oidc_auto_create") == "true")
