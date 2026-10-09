"""Two-step sign-in, single sign-on, LDAP and the sign-in record; shop orders (WooCommerce, ShipStation, Shopify and WooCommerce web hooks); the customer's quote page; refilling the shelf by itself."""
import base64
import hashlib
import hmac
import json
import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlmodel import Session, select
from starlette.testclient import TestClient

from app import auth, shop_orders, signin, stock
from app.db import engine
from app.models import ActivityLog, AppUser, Model3D, Order, QueueItem, StockItem, UserSecurity
from app.settings_store import get_setting, set_setting

PW = "a long enough password"
SETTING_KEYS = ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_auto_create", "oidc_role", "oidc_allowed_domains", "oidc_name", "oidc_public_url", "ldap_url", "ldap_dn", "ldap_role",
                "ldap_auto_create", "woo_url", "woo_key", "woo_secret", "shipstation_key", "shipstation_secret", "shop_sync", "shop_sync_last", "shop_hook_secret", "stock_auto_restock",
                "shop_name", "quote_currency", "notify_webhook_url")


def _stl():
    n = 30 + uuid.uuid4().int % 300
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 5}\n endloop\nendfacet\nendsolid t\n").encode() + uuid.uuid4().hex.encode()


def _model(c, name=None):
    r = c.post("/api/library/import", files={"file": (name or f"sq_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def _app():
    from app.main import app
    return app


def _fresh():
    auth._login_attempts.clear()
    return TestClient(_app())


def _login(c, name, password=PW, code=None):
    body = {"username": name, "password": password}
    if code is not None:
        body["code"] = code
    return c.post("/api/auth/login", json=body)


@pytest.fixture()
def tidy(authed):
    auth._login_attempts.clear()
    yield
    auth._login_attempts.clear()
    signin._states.clear()
    signin._discovery.clear()
    authed.put("/api/settings", json={k: "" for k in SETTING_KEYS})
    for u in authed.get("/api/users").json()["users"]:
        authed.delete(f"/api/users/{u['id']}")
    for o in authed.get("/api/orders").json()["orders"]:
        authed.delete(f"/api/orders/{o['id']}")
    for s in authed.get("/api/stock").json()["items"]:
        authed.delete(f"/api/stock/{s['id']}")
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")


@pytest.fixture()
def member(authed, tidy):
    name = "tfa" + uuid.uuid4().hex[:6]
    r = authed.post("/api/users", json={"username": name, "password": PW, "role": "member"})
    assert r.status_code == 200
    return name


# ---------------------------------------------------------------- the one-time code itself
def test_codes_follow_the_published_test_values():
    secret = base64.b32encode(b"12345678901234567890").decode()                   # RFC 6238, sha1
    assert signin.code_at(secret, 59) == "287082" and signin.code_at(secret, 1111111109) == "081804" and signin.code_at(secret, 20000000000) == "353130"
    assert signin.verify_code(secret, "287082", now=59) == 1
    assert signin.verify_code(secret, "287 082", now=59 + 30) == 1                 # one step late is fine, and spaces are ignored
    assert signin.verify_code(secret, "287082", now=59 + 90) is None               # three steps late is not
    assert signin.verify_code(secret, "287082", after_counter=1, now=59) is None   # already used
    assert signin.verify_code(secret, "28708", now=59) is None and signin.verify_code(secret, "abcdef", now=59) is None and signin.verify_code(secret, None, now=59) is None
    uri = signin.otpauth_uri("a b", secret)
    assert uri.startswith("otpauth://totp/Model%20Hub:a%20b?") and f"secret={secret}" in uri and "issuer=Model+Hub" in uri


# ---------------------------------------------------------------- two-step sign-in through the API
def test_two_step_signin_end_to_end(authed, member):
    c = _fresh()
    assert _login(c, member).status_code == 200
    me = c.get("/api/auth/me").json()
    assert me["totp"] is False and me["can_two_step"] is True
    assert c.post("/api/auth/2fa/enable", json={"code": "123456"}).status_code == 400                          # not started
    begun = c.post("/api/auth/2fa/begin").json()
    secret = begun["secret"]
    assert re.fullmatch(r"[A-Z2-7]{32}", secret) and begun["uri"].startswith("otpauth://totp/")
    assert c.post("/api/auth/2fa/enable", json={"code": "000000"}).status_code == 400
    assert get_setting(Session(engine), "auth_username") is not None
    step = signin.verify_code(secret, signin.code_at(secret))
    codes = c.post("/api/auth/2fa/enable", json={"code": signin.code_at(secret)}).json()["backup_codes"]
    assert len(codes) == 8 and all(re.fullmatch(r"[0-9a-f]{4}-[0-9a-f]{4}", x) for x in codes) and step is not None
    assert c.post("/api/auth/2fa/begin").status_code == 409                                                    # already on
    assert c.get("/api/auth/me").json()["totp"] is True
    with Session(engine) as s:
        row = signin.security_row(s, member)
        assert codes[0] not in (row.backup_json or "") and codes[0].replace("-", "") not in (row.backup_json or "")      # only hashes are kept
        counter = row.last_counter
    other = _fresh()
    r = _login(other, member)
    assert r.status_code == 401 and r.json()["totp_required"] is True and "modelhub_session" not in other.cookies
    assert _login(other, member, code="000000").status_code == 401
    assert _login(other, member, code=123456).status_code == 401                                               # a number, not text
    assert _login(other, member, code=signin._code(secret, counter)).status_code == 401                        # that step was used to set it up
    assert _login(other, member, code=signin._code(secret, counter + 1)).status_code == 200
    again = _fresh()
    assert _login(again, member, code=signin._code(secret, counter + 1)).status_code == 401                   # a code works once
    assert _login(again, member, code=codes[0]).status_code == 200                                             # a backup code works
    assert _login(_fresh(), member, code=codes[0]).status_code == 401                                          # ...once
    assert _login(_fresh(), member, code=codes[0].replace("-", "").upper()).status_code == 401                 # (already used; the format is forgiving, the code is not)
    assert _login(_fresh(), member, code=codes[1].replace("-", "").upper()).status_code == 200
    with Session(engine) as s:
        assert signin.backup_codes_left(s, member) == 6
    # the member cannot switch it off with the password alone, or with the code alone
    assert again.post("/api/auth/2fa/disable", json={"password": PW}).status_code == 401
    assert again.post("/api/auth/2fa/disable", json={"password": "wrong password!", "code": codes[2]}).status_code == 401
    assert again.post("/api/auth/2fa/disable", json={"password": PW, "code": codes[2]}).status_code == 200
    assert _login(_fresh(), member).status_code == 200


def test_the_administrator_can_reset_a_lost_second_step_and_removing_a_user_forgets_it(authed, member):
    c = _fresh()
    _login(c, member)
    secret = c.post("/api/auth/2fa/begin").json()["secret"]
    c.post("/api/auth/2fa/enable", json={"code": signin.code_at(secret)})
    users = authed.get("/api/users").json()["users"]
    mine = next(u for u in users if u["username"] == member)
    assert mine["two_step"] is True and mine["source"] == "local"
    assert authed.post(f"/api/users/{mine['id']}/two-step-reset").json() == {"reset": True}
    assert _login(_fresh(), member).status_code == 200
    assert authed.post("/api/users/999999/two-step-reset").status_code == 404
    c = _fresh()
    _login(c, member)
    secret = c.post("/api/auth/2fa/begin").json()["secret"]
    assert c.post("/api/auth/2fa/enable", json={"code": signin.code_at(secret)}).status_code == 200
    with Session(engine) as s:
        assert signin.security_row(s, member) is not None
    authed.delete(f"/api/users/{mine['id']}")
    with Session(engine) as s:
        assert signin.security_row(s, member) is None                              # a new user of that name starts with nothing
    authed.post("/api/users", json={"username": member, "password": PW, "role": "viewer"})
    assert _login(_fresh(), member).status_code == 200


def test_the_administrator_may_use_it_too_and_the_roles_that_cannot_write_still_can(authed, tidy):
    me = authed.get("/api/auth/me").json()
    assert me["role"] == "admin" and me["can_two_step"] is True
    authed.post("/api/users", json={"username": "viewer2fa", "password": PW, "role": "viewer"})
    authed.post("/api/users", json={"username": "printer2fa", "password": PW, "role": "printer"})
    for name in ("viewer2fa", "printer2fa"):
        c = _fresh()
        assert _login(c, name).status_code == 200
        assert c.post("/api/auth/2fa/begin").status_code == 200, name
        assert c.post("/api/library/import", files={"file": ("x.stl", _stl(), "application/octet-stream")}).status_code == 403
    tok = authed.post("/api/tokens", json={"name": "scripts", "scope": "write"}).json()["token"]
    api = TestClient(_app())
    assert api.post("/api/auth/2fa/begin", headers={"Authorization": f"Bearer {tok}"}).status_code == 400
    for t in authed.get("/api/tokens").json()["tokens"]:
        authed.delete(f"/api/tokens/{t['id']}")


# ---------------------------------------------------------------- the record of sign-ins
def test_sign_ins_are_recorded_for_the_administrator_only(authed, member):
    c = _fresh()
    assert _login(c, member, "wrong password!").status_code == 401
    assert _login(c, member).status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    mine = authed.get("/api/activity", params={"action": "security", "limit": 50}).json()["items"]
    text = " | ".join(i["summary"] for i in mine)
    assert f"Failed sign-in as {member}" in text and "Signed in from" in text and "Signed out" in text
    assert "wrong password" not in text and PW not in text                                                      # never a password
    assert all(i["actor"] for i in mine)
    cm = _fresh()
    _login(cm, member)
    seen = cm.get("/api/activity", params={"limit": 200}).json()
    assert seen["items"] and all(i["action"] != "security" for i in seen["items"])
    assert cm.get("/api/activity/export.csv").status_code == 403
    _login(_fresh(), "x\nevil=cmd|' /c calc'!A1", "whatever pass")
    r = authed.get("/api/activity/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "when (UTC),who,kind,what" and any("Failed sign-in" in l for l in lines)
    assert not any(l.split(",")[2:3] == ["security"] and "\n" in l for l in lines)
    only = authed.get("/api/activity/export.csv", params={"action": "security"}).text.splitlines()
    assert len(only) > 1 and all(",security," in l for l in only[1:])


# ---------------------------------------------------------------- LDAP
def _ldap(authed, role="viewer", create=""):
    authed.put("/api/settings", json={"ldap_url": "ldap://ldap.local", "ldap_dn": "uid={username},ou=people,dc=ex,dc=com", "ldap_role": role, "ldap_auto_create": create})


def test_a_directory_user_signs_in_and_gets_a_viewer_account(authed, tidy, monkeypatch):
    calls = []

    def fake(url, dn, password):
        calls.append((url, dn, password))
        return password == "directory pass"
    monkeypatch.setattr(signin, "_ldap_bind", fake)
    assert _login(_fresh(), "alice", "directory pass").status_code == 401 and calls == []                       # not set up: the directory is not asked
    _ldap(authed)
    assert _login(_fresh(), "alice", "wrong").status_code == 401
    assert calls[-1][:2] == ("ldap://ldap.local", "uid=alice,ou=people,dc=ex,dc=com")
    c = _fresh()
    assert _login(c, "alice", "directory pass").status_code == 200
    me = c.get("/api/auth/me").json()
    assert me["username"] == "alice" and me["role"] == "viewer" and me["source"] == "ldap"
    users = authed.get("/api/users").json()["users"]
    alice = next(u for u in users if u["username"] == "alice")
    assert alice["source"] == "ldap"
    with Session(engine) as s:
        assert s.exec(select(AppUser).where(AppUser.username == "alice")).first().password_hash.startswith("!")
    assert authed.patch(f"/api/users/{alice['id']}", json={"role": "member"}).json()["role"] == "member"
    assert authed.patch(f"/api/users/{alice['id']}", json={"password": PW}).status_code == 400                   # no local password for a directory account
    assert _login(_fresh(), "alice", "!whatever").status_code == 401                                              # the placeholder is not a password
    assert _fresh().post("/api/auth/login", json={"username": "alice", "password": PW}).status_code == 401
    assert _login(_fresh(), "alice", "directory pass").status_code == 200 and _fresh().get("/api/auth/me").status_code == 401
    assert authed.get("/api/activity", params={"action": "security", "limit": 100}).json()["items"]


def test_the_directory_cannot_take_over_a_local_account_or_the_administrator_or_use_an_empty_password(authed, member, monkeypatch):
    calls = []
    monkeypatch.setattr(signin, "_ldap_bind", lambda url, dn, password: calls.append(dn) or True)               # a directory that accepts anything
    _ldap(authed)
    assert _login(_fresh(), member, "not the local password").status_code == 401 and calls == []                # a local account is proved locally only
    admin = authed.get("/api/auth/me").json()["username"]
    assert _login(_fresh(), admin, "not the admin password").status_code == 401 and calls == []
    assert _login(_fresh(), "bob", "").status_code == 401 and calls == []                                       # an empty password is never sent
    for bad in ("a,b", "a b", "a)(uid=*", "../x", "x" * 80, "*"):
        assert _login(_fresh(), bad, "pass pass").status_code == 401, bad
    assert calls == []
    authed.put("/api/settings", json={"ldap_auto_create": "false"})
    assert _login(_fresh(), "carol", "pass pass").status_code == 401 and calls == ["uid=carol,ou=people,dc=ex,dc=com"]      # the directory said yes, but no account may be made
    assert not any(u["username"] == "carol" for u in authed.get("/api/users").json()["users"])


def test_only_plain_names_reach_the_directory_and_a_bad_address_is_refused(authed, tidy, monkeypatch):
    seen = []
    monkeypatch.setattr(signin, "_ldap_bind", lambda url, dn, password: seen.append((url, dn)) or False)
    _ldap(authed)
    for name in ("a+b", "a,b", "a=b", 'a"b', "a;b", "a\\b", "#a", "<a>"):
        _login(_fresh(), name, "pass pass")
    assert seen == []                                                            # nothing that could change the shape of the DN is ever sent
    _login(_fresh(), "ok.name-1@x", "pass pass")
    assert seen == [("ldap://ldap.local", "uid=ok.name-1@x,ou=people,dc=ex,dc=com")]
    seen.clear()
    authed.put("/api/settings", json={"ldap_url": "http://example.com"})
    _login(_fresh(), "dave", "pass pass")
    authed.put("/api/settings", json={"ldap_url": "ldap://ldap.local", "ldap_dn": "no placeholder here"})
    _login(_fresh(), "erin", "pass pass")
    assert seen == []
    with Session(engine) as s:
        assert signin.ldap_enabled(s) is False
    assert signin._clean('x\nevil=cmd|"y') == "x?evil?cmd??y"


def test_without_the_ldap_package_the_error_is_clear(authed, tidy):
    import builtins
    real = builtins.__import__

    def no_ldap(name, *a, **k):
        if name.startswith("ldap3"):
            raise ImportError("no ldap3")
        return real(name, *a, **k)
    builtins.__import__ = no_ldap
    try:
        with pytest.raises(signin.SignInError, match="ldap3"):
            signin._ldap_bind("ldap://x", "uid=a", "p")
    finally:
        builtins.__import__ = real


# ---------------------------------------------------------------- single sign-on
DISCOVERY = {"issuer": "https://idp.example", "authorization_endpoint": "https://idp.example/authorize", "token_endpoint": "https://idp.example/token", "userinfo_endpoint": "https://idp.example/userinfo"}


class Idp:
    def __init__(self, info):
        self.info, self.token_calls, self.fail_token = info, [], False

    def get(self, url, **kw):
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        assert url == DISCOVERY["userinfo_endpoint"] and kw["headers"]["Authorization"] == "Bearer at-123"
        return httpx.Response(200, json=self.info)

    def post(self, url, **kw):
        assert url == DISCOVERY["token_endpoint"]
        self.token_calls.append(kw["data"])
        return httpx.Response(400 if self.fail_token else 200, json={} if self.fail_token else {"access_token": "at-123", "token_type": "Bearer"})


def _sso(authed, monkeypatch, info, **settings):
    idp = Idp(info)
    monkeypatch.setattr(httpx, "get", idp.get)
    monkeypatch.setattr(httpx, "post", idp.post)
    authed.put("/api/settings", json={"oidc_issuer": "https://idp.example/", "oidc_client_id": "modelhub", "oidc_client_secret": "shh-secret", "oidc_name": "Authentik", **settings})
    return idp


def _begin(c):
    r = c.get("/api/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    return r.headers["location"], {k: v[0] for k, v in q.items()}


def test_single_sign_on_uses_the_code_flow_with_pkce(authed, tidy, monkeypatch):
    c = _fresh()
    assert c.get("/api/auth/oidc/start", follow_redirects=False).status_code == 404
    assert c.get("/api/auth/status").json()["sso"] is False
    idp = _sso(authed, monkeypatch, {"sub": "u1", "preferred_username": "frida", "email": "frida@ex.com", "email_verified": True}, oidc_auto_create="true", oidc_role="member")
    assert c.get("/api/auth/status").json() == {"configured": True, "sso": True, "sso_name": "Authentik"}
    location, q = _begin(c)
    assert location.startswith("https://idp.example/authorize?") and q["response_type"] == "code" and q["client_id"] == "modelhub" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"].endswith("/api/auth/oidc/callback") and q["scope"] == "openid profile email" and len(q["state"]) >= 20
    r = c.get("/api/auth/oidc/callback", params={"code": "abc", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/" and "modelhub_session" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    sent = idp.token_calls[0]
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "abc" and sent["client_secret"] == "shh-secret" and sent["redirect_uri"] == q["redirect_uri"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).decode().rstrip("=")
    assert challenge == q["code_challenge"]                                                                    # the verifier matches what the provider was shown
    me = c.get("/api/auth/me").json()
    assert (me["username"], me["role"], me["source"]) == ("frida", "member", "oidc")
    assert c.get("/api/settings").status_code == 403                                                           # a member, never the administrator
    again = _fresh()
    assert again.get("/api/auth/oidc/callback", params={"code": "abc", "state": q["state"]}, follow_redirects=False).headers["location"].startswith("/?sso_error=")   # a state works once


def test_single_sign_on_refusals(authed, tidy, monkeypatch):
    idp = _sso(authed, monkeypatch, {"sub": "u1", "preferred_username": "gina", "email": "gina@ex.com", "email_verified": True})

    def run(c=None, **params):
        c = c or _fresh()
        _, q = _begin(c)
        r = c.get("/api/auth/oidc/callback", params={"code": "abc", "state": q["state"], **params}, follow_redirects=False)
        return c, r

    c, r = run()
    assert r.headers["location"].startswith("/?sso_error=") and "no%20account" in r.headers["location"]
    assert "modelhub_session" not in r.headers.get("set-cookie", "")
    assert not any(u["username"] == "gina" for u in authed.get("/api/users").json()["users"])                   # nothing is made unless allowed
    authed.put("/api/settings", json={"oidc_auto_create": "true", "oidc_allowed_domains": "other.com, @corp.org"})
    assert run()[1].headers["location"].startswith("/?sso_error=")
    authed.put("/api/settings", json={"oidc_allowed_domains": "EX.com"})
    assert run()[1].headers["location"] == "/"
    authed.put("/api/settings", json={"oidc_role": "admin"})
    authed.delete(f"/api/users/{next(u['id'] for u in authed.get('/api/users').json()['users'] if u['username'] == 'gina')}")
    run()
    assert next(u for u in authed.get("/api/users").json()["users"] if u["username"] == "gina")["role"] == "viewer"      # "admin" is not a role this can give
    # a state that was never issued, a provider error, a refusal at the token step
    r = _fresh().get("/api/auth/oidc/callback", params={"code": "abc", "state": "made-up"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?sso_error=")
    c = _fresh()
    _, q = _begin(c)
    assert c.get("/api/auth/oidc/callback", params={"error": "access_denied", "state": q["state"]}, follow_redirects=False).headers["location"].startswith("/?sso_error=")
    idp.fail_token = True
    assert run()[1].headers["location"].startswith("/?sso_error=")
    idp.fail_token = False
    # the provider claims a name that belongs to the administrator or to a local account
    admin = authed.get("/api/auth/me").json()["username"]
    for taken in (admin, admin.upper()):
        idp.info.update({"preferred_username": taken})
        assert run()[1].headers["location"].startswith("/?sso_error=")
    authed.post("/api/users", json={"username": "localfolk", "password": PW, "role": "member"})
    idp.info.update({"preferred_username": "localfolk"})
    assert run()[1].headers["location"].startswith("/?sso_error=")
    idp.info.update({"preferred_username": "x y\nz"})
    assert run()[1].headers["location"].startswith("/?sso_error=")
    idp.info.update({"preferred_username": "", "email": "hana@ex.com", "email_verified": False})
    assert run()[1].headers["location"].startswith("/?sso_error=")
    idp.info.pop("sub")
    idp.info.update({"preferred_username": "hana2"})
    assert run()[1].headers["location"].startswith("/?sso_error=")


def test_a_provider_with_a_bad_configuration_is_refused(authed, tidy, monkeypatch):
    authed.put("/api/settings", json={"oidc_issuer": "ftp://idp.example", "oidc_client_id": "x"})
    assert _fresh().get("/api/auth/oidc/start", follow_redirects=False).status_code == 502
    bad = dict(DISCOVERY, token_endpoint="javascript:alert(1)")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json=bad))
    authed.put("/api/settings", json={"oidc_issuer": "https://idp2.example"})
    assert _fresh().get("/api/auth/oidc/start", follow_redirects=False).status_code == 502


def test_the_new_secrets_stay_secret(authed, tidy):
    authed.put("/api/settings", json={"oidc_client_secret": "sso-secret-value", "woo_secret": "cs_woo_value", "shipstation_secret": "ss-value", "shop_hook_secret": "hook-value"})
    s = authed.get("/api/settings").json()
    for k in ("oidc_client_secret", "woo_secret", "shipstation_secret", "shop_hook_secret"):
        assert s[k] == "********", k
    backup = authed.get("/api/backup").content
    for v in (b"sso-secret-value", b"cs_woo_value", b"ss-value", b"hook-value"):
        assert v not in backup
    import io
    import sqlite3
    import tempfile
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(backup))
    with tempfile.TemporaryDirectory() as d:
        z.extract("modelhub.db", d)
        db = sqlite3.connect(f"{d}/modelhub.db")
        assert db.execute("select count(*) from usersecurity").fetchone()[0] == 0 and db.execute("select count(*) from appuser").fetchone()[0] == 0
        db.close()


# ---------------------------------------------------------------- shop orders
def _woo(number=1001, sku="", name="Benchy"):
    return {"id": number + 5, "number": str(number), "billing": {"first_name": "Ann", "last_name": "Lee", "email": "ann@ex.com"}, "shipping": {"first_name": "", "last_name": ""},
            "line_items": [{"name": name, "sku": sku, "quantity": 2, "price": "7.50"}]}


def test_orders_from_each_shop_are_converted(authed):
    assert shop_orders.from_woocommerce(_woo(12, "benchy")) == ("woo-12", {"customer": "Ann Lee", "contact": "ann@ex.com", "lines": [{"item": "Benchy", "sku": "benchy", "quantity": 2, "price": 7.5}]})
    shop = {"id": 99, "name": "#1042", "email": "s@ex.com", "customer": {"first_name": "Sam", "last_name": "Roe"}, "shipping_address": {"name": "Sam R"},
            "line_items": [{"title": "Hook", "sku": "HK-1", "quantity": "3", "price": "2.00"}]}
    assert shop_orders.from_shopify(shop) == ("shopify-1042", {"customer": "Sam R", "contact": "s@ex.com", "lines": [{"item": "Hook", "sku": "HK-1", "quantity": 3, "price": 2.0}]})
    ss = {"orderNumber": "S-7", "customerEmail": "z@ex.com", "billTo": {"name": "Zed"}, "shipTo": {"name": ""}, "items": [{"name": "Gear", "sku": "G", "quantity": 1, "unitPrice": 4.5}, {"name": "Discount", "adjustment": True, "quantity": 1, "unitPrice": -1}]}
    key, group = shop_orders.from_shipstation(ss)
    assert key == "shipstation-S-7" and group["customer"] == "Zed" and len(group["lines"]) == 1
    assert shop_orders.from_woocommerce({}) is None and shop_orders.from_woocommerce("x") is None and shop_orders.from_shopify({"name": "#1"}) is None and shop_orders.from_shipstation({}) is None
    odd = shop_orders.from_woocommerce({"id": 1, "line_items": [{"name": "x" * 500, "quantity": "lots", "price": None}, "junk"]})
    assert odd[1]["lines"][0]["quantity"] == 1 and len(odd[1]["lines"][0]["item"]) == 200 and len(odd[1]["lines"]) == 1


def test_a_shop_order_becomes_one_order_and_is_never_made_twice(authed, tidy):
    m = _model(authed, "shopbenchy_%s.stl" % uuid.uuid4().hex[:5])
    sku = m["filename"].rsplit(".", 1)[0]
    with Session(engine) as s:
        report = shop_orders.ingest(s, [shop_orders.from_woocommerce(_woo(2001, sku)), shop_orders.from_woocommerce(_woo(2002, "", "Zzqx unmatchable gadget"))], "WooCommerce")
        assert [o["order"] for o in report["orders"]] == ["woo-2001", "woo-2002"] and report["matched"] == 1 and report["lines"] == 2
        again = shop_orders.ingest(s, [shop_orders.from_woocommerce(_woo(2001, sku))], "WooCommerce")
        assert again["orders"] == [] and again["skipped"] == ["woo-2001"]
    orders = authed.get("/api/orders").json()["orders"]
    assert len(orders) == 2 and all(o["status"] == "accepted" for o in orders)
    details = [authed.get(f"/api/orders/{o['id']}").json() for o in orders]
    detail = next(d for d in details if "[import:woo-2001]" in d["notes"])
    assert detail["customer"] == "Ann Lee" and "Imported from WooCommerce" in detail["notes"]
    assert any(i["filename"] == m["filename"] and i["quantity"] == 2 and i["unit_price"] == 7.5 for o in orders for i in authed.get(f"/api/orders/{o['id']}").json()["items"])
    assert any("Not matched to a model" in (authed.get(f"/api/orders/{o['id']}").json()["notes"] or "") for o in orders)
    authed.delete(f"/api/library/models/{m['id']}")


def test_the_csv_import_still_works_through_the_shared_step(authed, tidy):
    m = _model(authed, "csvmodel_%s.stl" % uuid.uuid4().hex[:5])
    sku = m["filename"].rsplit(".", 1)[0]
    csv_text = f"Name,Email,Lineitem name,Lineitem sku,Lineitem quantity,Lineitem price\n#9001,a@ex.com,Thing,{sku},3,5.00\n#9001,a@ex.com,Unknown part,,1,1.00\n"
    r = authed.post("/api/orders/import", files={"file": ("o.csv", csv_text.encode(), "text/csv")}).json()
    assert r["matched"] == 1 and r["lines"] == 2 and r["orders"][0]["order"] == "#9001" and r["dry"] is False
    assert authed.post("/api/orders/import", files={"file": ("o.csv", csv_text.encode(), "text/csv")}).json()["skipped"] == ["#9001"]
    authed.delete(f"/api/library/models/{m['id']}")


class Shops:
    def __init__(self):
        self.calls, self.status = [], {}

    def get(self, url, **kw):
        self.calls.append((url, kw))
        status = self.status.get("woo" if "wp-json" in url else "ss", 200)
        if status != 200:
            return httpx.Response(status, json={})
        if "wp-json" in url:
            return httpx.Response(200, json=[_woo(3001), _woo(3002)])
        return httpx.Response(200, json={"orders": [{"orderNumber": "SS-1", "billTo": {"name": "Zed"}, "items": [{"name": "Gear", "sku": "", "quantity": 1, "unitPrice": 1}]}]})


def test_woocommerce_and_shipstation_are_asked_with_their_keys_only_to_their_own_address(authed, tidy, monkeypatch):
    shops = Shops()
    monkeypatch.setattr(httpx, "get", shops.get)
    assert authed.post("/api/settings/shops/sync").status_code == 400
    authed.put("/api/settings", json={"woo_url": "https://shop.example/", "woo_key": "ck_1", "woo_secret": "cs_1", "shipstation_key": "k", "shipstation_secret": "s"})
    info = authed.get("/api/settings/shops").json()
    assert info["woocommerce"] and info["shipstation"] and info["sync_on"] is False and info["hook_url"] is None
    r = authed.post("/api/settings/shops/sync").json()["results"]
    assert r["WooCommerce"]["orders"][0]["order"] == "woo-3001" and len(r["WooCommerce"]["orders"]) == 2 and r["ShipStation"]["orders"][0]["order"] == "shipstation-SS-1"
    woo = next(c for c in shops.calls if "wp-json" in c[0])
    assert woo[0] == "https://shop.example/wp-json/wc/v3/orders" and woo[1]["auth"] == ("ck_1", "cs_1") and woo[1]["params"]["status"] == "processing,on-hold" and woo[1]["follow_redirects"] is False
    ss = next(c for c in shops.calls if "ssapi" in c[0])
    assert ss[0] == "https://ssapi.shipstation.com/orders" and ss[1]["auth"] == ("k", "s") and ss[1]["params"]["orderStatus"] == "awaiting_shipment"
    assert authed.post("/api/settings/shops/sync").json()["results"]["WooCommerce"]["orders"] == []             # the same orders are not made twice
    shops.status = {"woo": 401, "ss": 500}
    r = authed.post("/api/settings/shops/sync").json()["results"]
    assert "did not accept the key" in r["WooCommerce"]["error"] and "answered 500" in r["ShipStation"]["error"]
    assert json.loads(authed.get("/api/settings/shops").json()["last_sync"]) == {"WooCommerce": "error", "ShipStation": "error"}
    assert not any("ck_1" in json.dumps(a) for a in authed.get("/api/activity").json()["items"])


def test_a_shop_address_must_be_safe(authed, tidy, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda host: {"shop.lan": "192.168.1.5", "shop.pub": "93.184.216.34", "localhost": "127.0.0.1"}.get(host, "0.0.0.0"))
    ok = shop_orders._safe_shop_url
    assert ok("https://shop.example/") == "https://shop.example" and ok("http://shop.lan:8080") == "http://shop.lan:8080" and ok("http://localhost") == "http://localhost"
    for bad in ("http://shop.pub", "https://u:p@shop.example", "https://shop.example/?x=1", "https://shop.example/#f", "ftp://shop.example", "file:///etc/passwd", "https://", "nonsense"):
        assert ok(bad) is None, bad


def test_scheduled_sync_only_runs_when_switched_on(authed, tidy, monkeypatch):
    shops = Shops()
    monkeypatch.setattr(httpx, "get", shops.get)
    authed.put("/api/settings", json={"woo_url": "https://shop.example", "woo_key": "a", "woo_secret": "b"})
    from app import scheduler
    with Session(engine) as s:
        assert shop_orders.scheduled(s) is None and shops.calls == []
    authed.put("/api/settings", json={"shop_sync": "true"})
    with Session(engine) as s:
        assert set(shop_orders.scheduled(s)) == {"WooCommerce"}
    assert "shop_sync" in scheduler.INTERVALS and "restock" in scheduler.INTERVALS


def _signed(secret, body):
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_web_hooks_are_accepted_only_with_the_secret_address_and_a_correct_signature(authed, tidy):
    anon = TestClient(_app())
    info = authed.post("/api/settings/shops/hook-address", json={}).json()
    path = urlparse(info["hook_url"]).path
    assert path.startswith("/hooks/shop/") and len(path) > 30 and info["hook_secret_set"] is False
    assert authed.post("/api/settings/shops/hook-address", json={}).json()["hook_url"] == info["hook_url"]       # asking again keeps it
    assert "shop_hook_token" not in authed.get("/api/settings").json()
    body = json.dumps({"id": 5, "name": "#3001", "email": "q@ex.com", "customer": {"first_name": "Q", "last_name": "R"}, "line_items": [{"title": "Widget", "sku": "", "quantity": 1, "price": "3.00"}]}).encode()
    assert anon.post(path, content=body).status_code == 401                                                       # no secret set yet: nothing is accepted
    authed.put("/api/settings", json={"shop_hook_secret": "shared-secret"})
    assert anon.post(path, content=body).status_code == 401
    assert anon.post(path, content=body, headers={"X-Shopify-Hmac-Sha256": _signed("wrong", body)}).status_code == 401
    assert anon.post(path + "x", content=body, headers={"X-Shopify-Hmac-Sha256": _signed("shared-secret", body)}).status_code == 404
    r = anon.post(path, content=body, headers={"X-Shopify-Hmac-Sha256": _signed("shared-secret", body)})
    assert r.status_code == 200 and r.json() == {"made": 1, "skipped": 0}
    assert anon.post(path, content=body, headers={"X-Shopify-Hmac-Sha256": _signed("shared-secret", body)}).json() == {"made": 0, "skipped": 1}          # a retry changes nothing
    made = next(o for o in authed.get("/api/orders").json()["orders"] if o["customer"] == "Q R")
    assert made["status"] == "accepted"
    woo = json.dumps(_woo(4001)).encode()
    assert anon.post(path, content=woo, headers={"X-WC-Webhook-Signature": _signed("shared-secret", woo)}).json()["made"] == 1
    ping = b"webhook_id=12"                                                                                       # WooCommerce's first call
    assert anon.post(path, content=ping, headers={"X-WC-Webhook-Signature": _signed("shared-secret", ping)}).json() == {"made": 0, "skipped": 0}
    empty = json.dumps({"id": 8, "line_items": []}).encode()
    assert anon.post(path, content=empty, headers={"X-Shopify-Hmac-Sha256": _signed("shared-secret", empty)}).json()["made"] == 0
    assert anon.post(path, content=b"x" * (shop_orders.MAX_BODY + 1), headers={"X-Shopify-Hmac-Sha256": "a"}).status_code == 401
    new = authed.post("/api/settings/shops/hook-address", json={"rotate": True}).json()["hook_url"]
    assert new != info["hook_url"] and anon.post(path, content=body, headers={"X-Shopify-Hmac-Sha256": _signed("shared-secret", body)}).status_code == 404


# ---------------------------------------------------------------- the customer's quote
def _quote(authed, price=6.0):
    m = _model(authed, "quote_%s_widget.stl" % uuid.uuid4().hex[:4])
    order = authed.post("/api/orders", json={"customer": "Ivy Quote", "contact": "ivy@private.example", "notes": "internal: she haggles", "due_date": "2027-01-15",
                                             "items": [{"model_id": m["id"], "quantity": 3, "unit_price": price}]}).json()
    return m, order


def test_a_customer_sees_the_quote_and_nothing_else(authed, tidy):
    m, order = _quote(authed)
    assert order["quote_path"] is None
    link = authed.post(f"/api/orders/{order['id']}/quote-link", json={}).json()["quote_path"]
    assert re.fullmatch(r"/quote/[A-Za-z0-9_-]{30,}", link)
    assert authed.post(f"/api/orders/{order['id']}/quote-link", json={}).json()["quote_path"] == link          # asking again keeps the link
    authed.put("/api/settings", json={"shop_name": "Maker Corner", "quote_currency": "$"})
    anon = TestClient(_app())
    page = anon.get(link)
    assert page.status_code == 200 and "Accept this quote" in page.text and "noindex" in page.headers["x-robots-tag"] and page.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    data = anon.get(link + "/data").json()
    assert data["shop"] == "Maker Corner" and data["currency"] == "$" and data["customer"] == "Ivy Quote" and data["due_date"] == "2027-01-15" and data["can_answer"] is True
    assert data["items"] == [{"name": re.sub(r"[_-]+", " ", m["filename"].rsplit(".", 1)[0]), "quantity": 3, "unit_price": 6.0, "line_price": 18.0}] and data["total"] == 18.0
    text = json.dumps(data)
    for private in ("ivy@private.example", "haggles", "cost", "profit", "notes", "contact"):
        assert private not in text, private
    assert authed.get(f"/api/orders/{order['id']}").json()["quote_path"] == link
    for bad in ("/quote/nope", "/quote/" + "a" * 200, link[:-1] + ("a" if link[-1] != "a" else "b")):
        assert anon.get(bad + "/data").status_code == 404 and anon.get(bad).status_code == 404


def test_the_customer_can_accept_once_and_a_link_can_be_replaced_or_removed(authed, tidy):
    m, order = _quote(authed)
    link = authed.post(f"/api/orders/{order['id']}/quote-link", json={}).json()["quote_path"]
    anon = TestClient(_app())
    assert anon.post(link + "/respond", json={"accept": "yes"}).status_code == 400
    assert anon.post(link + "/respond", json={"accept": True, "name": 5}).status_code == 400
    r = anon.post(link + "/respond", json={"accept": True, "name": "Ivy\nQ", "message": "Please use blue"})
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    seen = authed.get(f"/api/orders/{order['id']}").json()
    assert seen["status"] == "accepted" and "accepted this quote online" in seen["notes"] and "signed: Ivy Q" in seen["notes"] and "Please use blue" in seen["notes"] and "haggles" in seen["notes"]
    assert anon.post(link + "/respond", json={"accept": False}).status_code == 409                                # answered once
    assert anon.get(link + "/data").json()["can_answer"] is False and "Accept this quote" in anon.get(link).text
    assert any("accepted online" in a["summary"] and a["actor"] == "the customer" for a in authed.get("/api/activity").json()["items"])
    assert authed.post(f"/api/orders/{order['id']}/quote-link", json={}).status_code == 409                       # only a quote gets a link
    m2, order2 = _quote(authed)
    first = authed.post(f"/api/orders/{order2['id']}/quote-link", json={}).json()["quote_path"]
    second = authed.post(f"/api/orders/{order2['id']}/quote-link", json={"rotate": True}).json()["quote_path"]
    assert first != second and anon.get(first + "/data").status_code == 404 and anon.get(second + "/data").status_code == 200
    r = anon.post(second + "/respond", json={"accept": False, "message": "too dear"})
    assert r.json() == {"status": "cancelled"} and authed.get(f"/api/orders/{order2['id']}").json()["status"] == "cancelled"
    m3, order3 = _quote(authed)
    third = authed.post(f"/api/orders/{order3['id']}/quote-link", json={}).json()["quote_path"]
    assert authed.delete(f"/api/orders/{order3['id']}/quote-link").json() == {"quote_path": None} and anon.get(third + "/data").status_code == 404
    assert authed.post("/api/orders/999999/quote-link", json={}).status_code == 404


def test_guessing_quote_links_is_slowed_down(authed, tidy):
    from app import quotes
    quotes._misses.clear()
    anon = TestClient(_app())
    codes = [anon.get(f"/quote/guess{i}/data").status_code for i in range(quotes.MISS_LIMIT + 3)]
    assert codes[0] == 404 and codes[-1] == 429
    quotes._misses.clear()


def test_a_quote_without_prices_says_so(authed, tidy):
    m = _model(authed)
    order = authed.post("/api/orders", json={"customer": "No Price", "items": [{"model_id": m["id"], "quantity": 1}]}).json()
    link = authed.post(f"/api/orders/{order['id']}/quote-link", json={}).json()["quote_path"]
    data = TestClient(_app()).get(link + "/data").json()
    assert data["items"][0]["line_price"] is None and data["total"] is None and data["unpriced"] is True


# ---------------------------------------------------------------- refilling the shelf
def test_the_shelf_refills_itself_only_when_asked_to(authed, tidy):
    m = _model(authed)
    item = authed.post("/api/stock", json={"model_id": m["id"], "on_hand": 5, "minimum": 4}).json()
    with Session(engine) as s:
        assert stock.auto_restock(s) == 0                                                                      # off by default
    authed.put("/api/settings", json={"stock_auto_restock": "true"})
    with Session(engine) as s:
        assert stock.auto_restock(s) == 0                                                                      # nothing is short yet
    authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": -3})                                          # a sale: 2 left, minimum 4, so 2 short: refilled on the spot
    queued = [q for q in authed.get("/api/queue").json() if q["model_id"] == m["id"]]
    assert len(queued) == 2 and all(q["notes"] == "For the shelf" for q in queued)
    assert authed.get("/api/stock").json()["items"][0]["queued"] == 2 and authed.get("/api/stock").json()["items"][0]["short"] == 0
    with Session(engine) as s:
        assert stock.auto_restock(s) == 0                                                                      # asking again adds nothing
    authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": -2})
    assert len([q for q in authed.get("/api/queue").json() if q["model_id"] == m["id"]]) == 4                    # 0 on hand, 2 queued, minimum 4: 2 more
    authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": 3})
    assert len([q for q in authed.get("/api/queue").json() if q["model_id"] == m["id"]]) == 4                    # putting some on does not queue
    free = authed.post("/api/stock", json={"model_id": _model(authed)["id"], "on_hand": 0, "minimum": 0}).json()
    with Session(engine) as s:
        assert stock.auto_restock(s) == 0 and not session_has_queue(s, free["model_id"])                        # no minimum, no refill


def session_has_queue(s, model_id):
    return bool(s.exec(select(QueueItem).where(QueueItem.model_id == model_id)).all())


def test_taking_parts_for_an_order_refills_the_shelf_and_one_round_is_capped(authed, tidy):
    m = _model(authed)
    item = authed.post("/api/stock", json={"model_id": m["id"], "on_hand": 6, "minimum": 5}).json()
    authed.put("/api/settings", json={"stock_auto_restock": "true"})
    order = authed.post("/api/orders", json={"customer": "Shelf", "items": [{"model_id": m["id"], "quantity": 4}]}).json()
    assert authed.post(f"/api/orders/{order['id']}/take-from-stock").json()["taken"] == 4
    assert len([q for q in authed.get("/api/queue").json() if q["model_id"] == m["id"]]) == 3                    # 2 left, minimum 5
    big = authed.post("/api/stock", json={"model_id": _model(authed)["id"], "on_hand": 0, "minimum": 90}).json()
    with Session(engine) as s:
        before = len(s.exec(select(QueueItem)).all())
        made = stock.auto_restock(s)
        assert made <= stock.AUTO_RESTOCK_CAP and len(s.exec(select(QueueItem)).all()) == before + made
    assert authed.post(f"/api/stock/{big['id']}/make", json={"quantity": 500}).status_code == 400              # the manual button keeps its own limit


def test_the_new_events_can_be_switched_and_the_controls_are_in_the_page():
    from pathlib import Path
    from app.notify import EVENTS
    assert "order_new" in EVENTS and "quote_answer" in EVENTS
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html, js = (root / "index.html").read_text(encoding="utf-8"), (root / "app.js").read_text(encoding="utf-8")
    for element in ("signin-box", "shops-box", "login-code", "stock-auto-restock"):
        assert f'id="{element}"' in html, element
    for needle in ("/api/auth/2fa/begin", "/api/auth/2fa/enable", "/api/settings/shops", "quote-link", "oidc_issuer", "ldap_url", "totp_required", "two-step-reset"):
        assert needle in js, needle
    assert "/api/activity/export.csv" in html
