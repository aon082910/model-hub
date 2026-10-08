"""More than one login: members, read-only viewers, and what each may do."""
import io
import sqlite3
import zipfile

import pytest
from starlette.testclient import TestClient

from app import auth
from app.config import DB_PATH

PASSWORD = "a long enough password"


@pytest.fixture(autouse=True)
def clean_users(authed):
    yield
    for u in authed.get("/api/users").json()["users"]:
        authed.delete(f"/api/users/{u['id']}")
    auth._login_attempts.clear()


def _new_user(c, name, role="member", password=PASSWORD):
    r = c.post("/api/users", json={"username": name, "password": password, "role": role})
    assert r.status_code == 200, r.text
    return r.json()


def _sign_in(name, password=PASSWORD):
    from app.main import app
    client = TestClient(app)
    r = client.post("/api/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return client


def test_the_administrator_adds_lists_and_removes_logins(authed):
    a = _new_user(authed, "mum")
    b = _new_user(authed, "kid", role="viewer")
    assert a["role"] == "member" and b["role"] == "viewer" and "password_hash" not in a
    listing = authed.get("/api/users").json()
    assert [u["username"] for u in listing["users"]] == ["kid", "mum"] and listing["admin"] and listing["roles"] == ["member", "viewer"]
    assert authed.delete(f"/api/users/{a['id']}").status_code == 200
    assert authed.delete(f"/api/users/{a['id']}").status_code == 404
    assert [u["username"] for u in authed.get("/api/users").json()["users"]] == ["kid"]


@pytest.mark.parametrize("payload,status", [
    ({"username": "", "password": PASSWORD}, 400), ({"username": "has space", "password": PASSWORD}, 400),
    ({"username": "-leading", "password": PASSWORD}, 400), ({"username": "x" * 41, "password": PASSWORD}, 400),
    ({"username": 5, "password": PASSWORD}, 400), ({"username": "okname", "password": "short"}, 400),
    ({"username": "okname", "password": PASSWORD, "role": "admin"}, 400), ({"username": "okname"}, 400),
])
def test_bad_new_logins_are_refused(authed, payload, status):
    assert authed.post("/api/users", json=payload).status_code == status
    assert authed.get("/api/users").json()["users"] == []


def test_names_cannot_collide_with_each_other_or_the_administrator(authed):
    _new_user(authed, "Casey")
    assert authed.post("/api/users", json={"username": "casey", "password": PASSWORD}).status_code == 409
    admin = authed.get("/api/users").json()["admin"]
    assert authed.post("/api/users", json={"username": admin.upper(), "password": PASSWORD}).status_code == 409


def test_a_member_signs_in_and_uses_the_library_but_not_the_administration(authed):
    _new_user(authed, "memberone")
    member = _sign_in("memberone")
    assert member.get("/api/auth/me").json() == {"username": "memberone", "role": "member"}
    assert member.get("/api/library/models").status_code == 200
    made = member.post("/api/collections", json={"name": "made by a member"})
    assert made.status_code == 200
    member.delete(f"/api/collections/{made.json()['id']}")
    for method, path in (("get", "/api/settings"), ("get", "/api/settings/extension-key"), ("put", "/api/settings"),
                         ("get", "/api/backup"), ("post", "/api/backup/save"), ("get", "/api/users"), ("post", "/api/users"),
                         ("get", "/api/printers"), ("post", "/api/duplicates/merge"), ("post", "/api/duplicates/merge-all"),
                         ("post", "/api/library/non-model-files/remove")):
        r = getattr(member, method)(path, **({"json": {}} if method in ("post", "put") else {}))
        assert r.status_code == 403 and "administrator" in r.json()["detail"], (method, path, r.status_code)
    assert member.get("/api/duplicates").status_code == 200              # looking is fine
    assert authed.get("/api/auth/me").json()["role"] == "admin"


def test_a_viewer_can_look_but_not_change_anything(authed):
    _new_user(authed, "watcher", role="viewer")
    viewer = _sign_in("watcher")
    assert viewer.get("/api/library/models").status_code == 200
    assert viewer.get("/api/projects").status_code == 200
    for method, path in (("post", "/api/collections"), ("post", "/api/projects"), ("put", "/api/settings")):
        r = getattr(viewer, method)(path, json={"name": "x"})
        assert r.status_code == 403, (method, path)
    r = viewer.post("/api/collections", json={"name": "nope"})
    assert "read-only" in r.json()["detail"]
    assert viewer.get("/api/settings").status_code == 403
    assert viewer.post("/api/auth/logout").status_code == 200


def test_changing_a_role_or_deleting_a_user_takes_effect_at_once(authed):
    user = _new_user(authed, "shifty")
    session = _sign_in("shifty")
    assert session.post("/api/collections", json={"name": "member made this"}).status_code == 200
    authed.patch(f"/api/users/{user['id']}", json={"role": "viewer"})
    assert session.post("/api/collections", json={"name": "now read only"}).status_code == 403
    assert session.get("/api/auth/me").json()["role"] == "viewer"
    authed.delete(f"/api/users/{user['id']}")
    assert session.get("/api/library/models").status_code == 401
    for c in authed.get("/api/collections").json():
        if c["name"] == "member made this":
            authed.delete(f"/api/collections/{c['id']}")


def test_wrong_logins_are_refused(authed):
    _new_user(authed, "carefulone")
    from app.main import app
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "carefulone", "password": "wrong password!"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "nobody-here", "password": PASSWORD}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "", "password": ""}).status_code == 401
    assert c.get("/api/library/models").status_code == 401
    auth._login_attempts.clear()


def test_a_password_can_be_reset_by_the_administrator_and_changed_by_the_user(authed):
    user = _new_user(authed, "forgetful")
    authed.patch(f"/api/users/{user['id']}", json={"password": "a brand new password"})
    from app.main import app
    assert TestClient(app).post("/api/auth/login", json={"username": "forgetful", "password": PASSWORD}).status_code == 401
    c = _sign_in("forgetful", "a brand new password")
    assert c.post("/api/auth/me/password", json={"current_password": "wrong", "new_password": "another good password"}).status_code == 401
    assert c.post("/api/auth/me/password", json={"current_password": "a brand new password", "new_password": "short"}).status_code == 400
    assert c.post("/api/auth/me/password", json={"current_password": "a brand new password", "new_password": "another good password"}).status_code == 200
    _sign_in("forgetful", "another good password")
    assert authed.patch(f"/api/users/{user['id']}", json={"password": "short"}).status_code == 400
    assert authed.patch(f"/api/users/{user['id']}", json={"role": "boss"}).status_code == 400


def test_the_administrator_has_their_own_password_route(authed):
    assert authed.post("/api/auth/me/password", json={"current_password": "x", "new_password": "long enough pass"}).status_code == 400
    assert authed.get("/api/auth/me").json()["role"] == "admin"


def test_a_viewer_can_still_change_their_own_password(authed):
    _new_user(authed, "readonlyone", role="viewer")
    viewer = _sign_in("readonlyone")
    r = viewer.post("/api/auth/me/password", json={"current_password": PASSWORD, "new_password": "my new viewer password"})
    assert r.status_code == 200


def test_logins_are_not_part_of_a_backup_and_a_restore_leaves_them_alone(authed):
    _new_user(authed, "stays")
    data = authed.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extract("modelhub.db", path=str(DB_PATH.parent / "peek_users"))
    conn = sqlite3.connect(DB_PATH.parent / "peek_users" / "modelhub.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM appuser").fetchone()[0] == 0
    finally:
        conn.close()
    r = authed.post("/api/backup/restore", files={"file": ("b.zip", data)}, data={"confirm": "replace"})
    assert r.status_code == 200
    assert [u["username"] for u in authed.get("/api/users").json()["users"]] == ["stays"]
    _sign_in("stays")
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


def test_the_extension_key_still_only_opens_the_import_door(authed):
    key = authed.get("/api/settings/extension-key").json()["extension_api_key"]
    from app.main import app
    c = TestClient(app)
    assert c.get("/api/library/models", headers={"X-Model-Hub-Api-Key": key}).status_code == 401
    assert c.get("/api/users", headers={"X-Model-Hub-Api-Key": key}).status_code == 401


def test_only_signed_in_people_reach_the_user_list(authed):
    from app.main import app
    assert TestClient(app).get("/api/users").status_code == 401
    assert TestClient(app).get("/api/auth/me").status_code == 401
