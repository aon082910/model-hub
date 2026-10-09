"""The statistics page, the update notice, personal API tokens, and the HTTPS-only cookie behind a proxy."""
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from starlette.testclient import TestClient

from app import notify as notify_module
from app import stats, tokens, update_check, version
from app import scheduler

PW = "a long enough password"


def _anon():
    from app.main import app
    return TestClient(app)


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture(autouse=True)
def clean(authed, monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module.httpx, "post", lambda url, **kw: sent.append((kw["json"]["title"], kw["json"]["message"])))
    authed.put("/api/settings", json={"notify_webhook_url": "http://hook.example/x"})
    yield sent
    for t in authed.get("/api/tokens").json()["tokens"]:
        authed.delete(f"/api/tokens/{t['id']}")
    authed.put("/api/settings", json={"notify_webhook_url": "", "update_check": "", "update_latest": "", "update_notified": "",
                                      "update_checked_at": "", "update_url": "", "notify_update_available": ""})


# ---------- statistics ----------

def _session():
    from sqlmodel import Session
    from app.db import engine
    return Session(engine)


def test_stats_add_up_the_print_log(authed):
    spool = authed.post("/api/filament", json={"material": "PETG", "spool_weight_g": 1000, "remaining_g": 800, "cost": 20}).json()
    tag = uuid.uuid4().hex[:5]
    m1 = authed.post("/api/library/import", files={"file": (f"{tag}_a.stl", _stl(33 + int(tag[:2], 16) % 50), "application/octet-stream")}).json()
    m2 = authed.post("/api/library/import", files={"file": (f"{tag}_b.stl", _stl(90 + int(tag[:2], 16) % 50), "application/octet-stream")}).json()
    this_month = datetime.utcnow().strftime("%Y-%m-15")
    old_month = (datetime.utcnow().replace(day=1) - timedelta(days=40)).strftime("%Y-%m-10")
    try:
        before = authed.get("/api/stats").json()["totals"]
        for model, when, grams, minutes in ((m1, this_month, 100, 60), (m1, this_month, 50, 30), (m2, old_month, 200, 120)):
            authed.post("/api/prints", json={"model_id": model["id"], "filament_id": spool["id"], "grams": grams, "minutes": minutes,
                                             "printed_at": when, "deduct": False})
        data = authed.get("/api/stats", params={"months": 6}).json()
        after = data["totals"]
        assert after["prints"] - before["prints"] == 3
        assert round(after["grams"] - before["grams"], 1) == 350 and round(after["hours"] - before["hours"], 1) == 3.5
        assert round(after["cost"] - before["cost"], 2) == 7.0                        # 350 g of a 20 per kg spool
        months = {r["month"]: r for r in data["per_month"]}
        assert len(data["per_month"]) == 6 and data["per_month"][-1]["month"] == this_month[:7]
        assert months[this_month[:7]]["prints"] >= 2 and months[old_month[:7]]["prints"] >= 1
        assert months[this_month[:7]]["models_added"] >= 2
        top = {t["filename"]: t["prints"] for t in data["top_models"]}
        assert top[m1["filename"]] == 2 and top[m2["filename"]] == 1
        assert any(x["material"] == "PETG" and x["grams"] >= 350 for x in data["materials"])
        assert data["library"]["models"] >= 2 and data["library"]["ever_printed"] >= 2
        assert authed.get("/api/stats", params={"months": 1}).json()["totals"]["prints"] <= after["prints"]
    finally:
        for m in (m1, m2):
            authed.delete(f"/api/library/models/{m['id']}")
        authed.delete(f"/api/filament/{spool['id']}")


def test_month_keys_cross_year_boundaries():
    assert stats._month_keys(datetime(2026, 2, 10), 4) == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert stats._month_keys(datetime(2026, 12, 31), 1) == ["2026-12"]


def test_success_rate_comes_from_printer_events(authed):
    from app.models import PrinterJob
    with _session() as s:
        for outcome in ("done", "done", "done", "stopped"):
            s.add(PrinterJob(printer_id=9999, filename="x.gcode", outcome=outcome, finished_at=datetime.utcnow()))
        s.commit()
        ids = [j.id for j in s.query(PrinterJob).filter(PrinterJob.printer_id == 9999)] if hasattr(s, "query") else []
    try:
        jobs = authed.get("/api/stats").json()["printer_jobs"]
        assert jobs["done"] >= 3 and jobs["stopped"] >= 1 and jobs["success_rate"] is not None
    finally:
        with _session() as s:
            from sqlmodel import select
            for j in s.exec(select(PrinterJob).where(PrinterJob.printer_id == 9999)).all():
                s.delete(j)
            s.commit()


@pytest.mark.parametrize("months", [0, 61, "x"])
def test_months_must_be_sensible(authed, months):
    assert authed.get("/api/stats", params={"months": months}).status_code == 422


def test_stats_are_for_everyone_signed_in_but_nobody_else(authed):
    authed.post("/api/users", json={"username": "statsviewer", "password": PW, "role": "viewer"})
    try:
        viewer = _anon()
        viewer.post("/api/auth/login", json={"username": "statsviewer", "password": PW})
        assert viewer.get("/api/stats").status_code == 200
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert _anon().get("/api/stats").status_code == 401


# ---------- versions and the update notice ----------

@pytest.mark.parametrize("value,expected", [("v2.9.0", (2, 9, 0)), ("2.10.1", (2, 10, 1)), ("v2.9", None), ("latest", None), ("", None), (None, None), ("v2.9.0-rc1", None)])
def test_version_parsing(value, expected):
    assert version.parse(value) == expected


def test_newer_is_compared_as_numbers_not_text():
    assert version.is_newer("v2.10.0", "2.9.0") and not version.is_newer("v2.9.0", "2.10.0")
    assert not version.is_newer("v2.9.0", "2.9.0") and not version.is_newer("junk", "2.9.0") and not version.is_newer("v2.9.0", "dev")


def test_the_running_version_is_visible_to_everyone_but_update_details_only_to_the_admin(authed, monkeypatch):
    monkeypatch.setattr(version, "VERSION", "2.8.0")
    admin = authed.get("/api/system/version").json()
    assert admin["current"] == "2.8.0" and "update_available" in admin
    authed.post("/api/users", json={"username": "versionmember", "password": PW})
    try:
        member = _anon()
        member.post("/api/auth/login", json={"username": "versionmember", "password": PW})
        assert member.get("/api/system/version").json() == {"current": "2.8.0"}
        assert member.post("/api/system/update-check").status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


def _github(monkeypatch, tag="v9.9.9", status=200, body=None):
    def fake_get(url, **kw):
        assert url == update_check.RELEASES_API and "Authorization" not in kw["headers"]
        return httpx.Response(status, json=body if body is not None else {"tag_name": tag, "html_url": f"https://github.com/aon082910/model-hub/releases/tag/{tag}"})
    monkeypatch.setattr(update_check.httpx, "get", fake_get)


def test_a_newer_release_is_reported_and_announced_once(authed, clean, monkeypatch):
    monkeypatch.setattr(version, "VERSION", "2.8.0")
    _github(monkeypatch, "v2.9.0")
    first = authed.post("/api/system/update-check").json()
    assert first["latest"] == "v2.9.0" and first["update_available"] is True and first["checked_at"]
    assert first["url"].startswith("https://github.com/")
    assert [m for m in clean if "update available" in m[0]] and len([m for m in clean if "update available" in m[0]]) == 1
    authed.post("/api/system/update-check")
    assert len([m for m in clean if "update available" in m[0]]) == 1                   # once per version
    _github(monkeypatch, "v2.9.1")
    authed.post("/api/system/update-check")
    assert len([m for m in clean if "update available" in m[0]]) == 2


def test_being_up_to_date_is_not_an_update(authed, clean, monkeypatch):
    monkeypatch.setattr(version, "VERSION", "2.9.0")
    _github(monkeypatch, "v2.9.0")
    assert authed.post("/api/system/update-check").json()["update_available"] is False
    assert not [m for m in clean if "update" in m[0]]


@pytest.mark.parametrize("status,body,fragment", [(403, None, "rate limiting"), (500, None, "error"), (200, {"tag_name": "nightly"}, "unexpected"), (200, {"nope": 1}, "unexpected")])
def test_github_trouble_becomes_a_message(authed, monkeypatch, status, body, fragment):
    _github(monkeypatch, status=status, body=body)
    r = authed.post("/api/system/update-check")
    assert r.status_code == 502 and fragment in r.json()["detail"]


def test_an_unreachable_github_is_a_message_too(authed, monkeypatch):
    def down(url, **kw):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(update_check.httpx, "get", down)
    assert authed.post("/api/system/update-check").status_code == 502


def test_the_scheduled_check_respects_the_switch_and_never_raises(authed, monkeypatch):
    calls = []
    monkeypatch.setattr(update_check, "fetch_latest", lambda: calls.append(1) or {"tag": "v9.0.0", "url": update_check.RELEASES_PAGE})
    with _session() as s:
        update_check.scheduled(s)
        assert calls == [1]
        authed.put("/api/settings", json={"update_check": "false"})
        update_check.scheduled(s)
        assert calls == [1]
    def broken():
        raise update_check.UpdateCheckError("offline")
    monkeypatch.setattr(update_check, "fetch_latest", broken)
    authed.put("/api/settings", json={"update_check": "true"})
    with _session() as s:
        update_check.scheduled(s)


def test_the_scheduler_has_the_update_job():
    assert "updates" in scheduler.INTERVALS and "updates" in scheduler._jobs()


# ---------- API tokens ----------

def _token(c, **fields):
    r = c.post("/api/tokens", json={"name": "script", **fields})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_token_is_shown_once_and_stored_only_as_a_hash(authed):
    t = _token(authed, scope="read")
    assert t["token"].startswith("mh_") and t["prefix"] == t["token"][:10] and t["scope"] == "read"
    listed = authed.get("/api/tokens").json()["tokens"][0]
    assert "token" not in listed and listed["prefix"] == t["prefix"]
    from app.models import ApiToken
    with _session() as s:
        row = s.get(ApiToken, t["id"])
        assert row.token_hash == tokens.hash_token(t["token"]) and t["token"] not in str(row.model_dump())


def test_a_read_token_can_look_but_not_change_and_cannot_see_settings(authed):
    t = _token(authed, scope="read")
    headers = {"Authorization": f"Bearer {t['token']}"}
    c = _anon()
    assert c.get("/api/library/models", headers=headers).status_code == 200
    assert c.get("/api/stats", headers=headers).status_code == 200
    assert c.post("/api/collections", json={"name": "x"}, headers=headers).status_code == 403
    assert c.get("/api/settings", headers=headers).status_code == 403
    assert c.get("/api/tokens", headers=headers).status_code == 403
    me = c.get("/api/auth/me", headers=headers).json()
    assert (me["username"], me["role"], me["can_two_step"]) == ("token:script", "viewer", False)


def test_a_write_token_acts_like_a_member(authed):
    t = _token(authed, scope="write", name="home automation")
    headers = {"Authorization": f"Bearer {t['token']}"}
    c = _anon()
    made = c.post("/api/collections", json={"name": "made by a token"}, headers=headers)
    assert made.status_code == 200
    authed.delete(f"/api/collections/{made.json()['id']}")
    assert c.get("/api/settings", headers=headers).status_code == 403
    assert c.post("/api/backup/save", headers=headers).status_code == 403
    assert c.post("/api/tokens", json={"name": "n"}, headers=headers).status_code == 403          # a token cannot mint tokens


def test_an_import_token_only_opens_the_import_door(authed):
    t = _token(authed, scope="import")
    headers = {"Authorization": f"Bearer {t['token']}"}
    c = _anon()
    r = c.post("/api/library/import", files={"file": (f"tok_{uuid.uuid4().hex[:5]}.stl", _stl(177), "application/octet-stream")}, headers=headers)
    assert r.status_code == 200
    authed.delete(f"/api/library/models/{r.json()['id']}")
    assert c.get("/api/library/models", headers=headers).status_code == 401


def test_revoked_expired_and_made_up_tokens_are_refused(authed):
    t = _token(authed, scope="read")
    headers = {"Authorization": f"Bearer {t['token']}"}
    c = _anon()
    assert c.get("/api/library/models", headers=headers).status_code == 200
    assert authed.delete(f"/api/tokens/{t['id']}").status_code == 200
    assert c.get("/api/library/models", headers=headers).status_code == 401
    assert authed.delete(f"/api/tokens/{t['id']}").status_code == 404
    e = _token(authed, scope="read", expires_days=1)
    from app.models import ApiToken
    with _session() as s:
        row = s.get(ApiToken, e["id"])
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        s.add(row)
        s.commit()
    assert c.get("/api/library/models", headers={"Authorization": f"Bearer {e['token']}"}).status_code == 401
    for bad in ("Bearer mh_notarealtoken", "Bearer ", "Bearer " + "x" * 300, "Basic abc", "mh_alone"):
        assert c.get("/api/library/models", headers={"Authorization": bad}).status_code == 401


def test_use_is_noted(authed):
    t = _token(authed, scope="read")
    assert authed.get("/api/tokens").json()["tokens"][0]["last_used_at"] is None
    _anon().get("/api/library/models", headers={"Authorization": f"Bearer {t['token']}"})
    assert authed.get("/api/tokens").json()["tokens"][0]["last_used_at"] is not None


@pytest.mark.parametrize("payload", [{}, {"name": " "}, {"name": 5}, {"name": "n", "scope": "root"}, {"name": "n", "expires_days": 0},
                                     {"name": "n", "expires_days": "soon"}, {"name": "n", "expires_days": True}])
def test_bad_tokens_are_refused(authed, payload):
    assert authed.post("/api/tokens", json=payload).status_code == 400


def test_tokens_are_not_in_backups_and_survive_a_restore(authed):
    import io
    import sqlite3
    import zipfile
    from app.config import DB_PATH
    t = _token(authed, scope="read")
    data = authed.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extract("modelhub.db", path=str(DB_PATH.parent / "peek_tokens"))
    conn = sqlite3.connect(DB_PATH.parent / "peek_tokens" / "modelhub.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM apitoken").fetchone()[0] == 0
    finally:
        conn.close()
    assert authed.post("/api/backup/restore", files={"file": ("b.zip", data)}, data={"confirm": "replace"}).status_code == 200
    assert _anon().get("/api/library/models", headers={"Authorization": f"Bearer {t['token']}"}).status_code == 200
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


# ---------- the session cookie behind HTTPS ----------

def test_the_session_cookie_is_https_only_behind_an_https_proxy(authed):
    from app import auth
    authed.post("/api/users", json={"username": "cookieuser", "password": PW})
    try:
        c = _anon()
        r = c.post("/api/auth/login", json={"username": "cookieuser", "password": PW})
        assert r.status_code == 200 and "secure" not in r.headers["set-cookie"].lower()
        auth._login_attempts.clear()
        r = c.post("/api/auth/login", json={"username": "cookieuser", "password": PW}, headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
        r = c.post("/api/auth/login", json={"username": "cookieuser", "password": PW}, headers={"X-Forwarded-Proto": "http"})
        assert "secure" not in r.headers["set-cookie"].lower()
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
        auth._login_attempts.clear()
