"""The maintenance history, reusable print profiles, and the read-only status page for a wall display."""
import json
import uuid

import pytest
from starlette.testclient import TestClient

from app import printwatch

PW = "a long enough password"


def _stl():
    n = 30 + uuid.uuid4().int % 300
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 4}\n endloop\nendfacet\nendsolid t\n").encode() + uuid.uuid4().hex.encode()


def _model(c):
    r = c.post("/api/library/import", files={"file": (f"lp_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def tidy(authed):
    yield
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")
    for p in authed.get("/api/profiles").json()["profiles"]:
        authed.delete(f"/api/profiles/{p['id']}")
    authed.post("/api/settings/status-page", json={"enabled": False, "show_files": False})
    printwatch.reset()


# ---------------------------------------------------------------- maintenance history
def _task(c, name="Oil the rails"):
    p = c.post("/api/printers", json={"name": "Hist", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    t = c.post("/api/maintenance", json={"printer_id": p["id"], "name": name, "every_days": 30}).json()
    return p, t


def test_marking_a_task_done_keeps_a_history_with_the_note(authed, tidy):
    p, t = _task(authed)
    assert authed.get("/api/maintenance/log").json()["log"] == []
    first = authed.post(f"/api/maintenance/{t['id']}/done", json={"note": "  used PTFE grease  "})
    assert first.status_code == 200 and first.json()["status"] == "ok"
    authed.post(f"/api/maintenance/{t['id']}/done")                                  # no body at all still works
    rows = authed.get("/api/maintenance/log").json()["log"]
    assert [r["note"] for r in rows] == [None, "used PTFE grease"] and rows[0]["printer"] == "Hist" and rows[0]["name"] == "Oil the rails"
    assert rows[0]["print_hours"] == 0 and rows[0]["done_at"]
    assert authed.post(f"/api/maintenance/{t['id']}/done", json={"note": 5}).status_code == 400
    assert len(authed.get("/api/maintenance/log", params={"printer_id": p["id"]}).json()["log"]) == 2
    assert authed.get("/api/maintenance/log", params={"printer_id": 999999}).json()["log"] == []
    assert len(authed.get("/api/maintenance/log", params={"limit": 1}).json()["log"]) == 1


def test_the_history_outlives_the_task_but_not_the_printer(authed, tidy):
    p, t = _task(authed)
    authed.post(f"/api/maintenance/{t['id']}/done", json={"note": "kept"})
    authed.delete(f"/api/maintenance/{t['id']}")
    rows = authed.get("/api/maintenance/log").json()["log"]
    assert [r["note"] for r in rows] == ["kept"]                                        # the task is gone, what was done is not
    assert authed.delete(f"/api/maintenance/log/{rows[0]['id']}").json() == {"status": "deleted"}
    assert authed.delete("/api/maintenance/log/999999").status_code == 404
    p2, t2 = _task(authed, "Another")
    authed.post(f"/api/maintenance/{t2['id']}/done")
    authed.delete(f"/api/printers/{p2['id']}")
    assert authed.get("/api/maintenance/log").json()["log"] == []                       # a removed printer takes its history with it


def test_a_viewer_can_read_the_history_but_not_write_to_it(authed, tidy):
    from app.main import app
    p, t = _task(authed)
    authed.post(f"/api/maintenance/{t['id']}/done")
    authed.post("/api/users", json={"username": "lpviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "lpviewer", "password": PW})
        assert viewer.get("/api/maintenance/log").status_code == 200
        assert viewer.post(f"/api/maintenance/{t['id']}/done").status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


# ---------------------------------------------------------------- print profiles
def test_a_profile_is_checked_saved_renamed_and_removed(authed, tidy):
    assert authed.post("/api/profiles", json={"name": "", "settings": {"material": "PETG"}}).status_code == 400
    assert authed.post("/api/profiles", json={"name": "x", "settings": {}}).status_code == 400
    assert authed.post("/api/profiles", json={"name": "x", "settings": {"colour": "red"}}).status_code == 400
    assert authed.post("/api/profiles", json={"name": "x", "settings": {"material": ["a"]}}).status_code == 400
    assert authed.post("/api/profiles", json={"name": "x", "settings": {"material": "P" * 90}}).status_code == 400
    made = authed.post("/api/profiles", json={"name": "PETG draft", "settings": {"material": "PETG", "layer_height": 0.28, "infill": "", "nozzle_temp": "240"}}).json()
    assert made["settings"] == {"material": "PETG", "layer_height": "0.28", "nozzle_temp": "240"}
    assert authed.post("/api/profiles", json={"name": "petg DRAFT", "settings": {"material": "PLA"}}).status_code == 400          # names are not case-sensitive
    renamed = authed.put(f"/api/profiles/{made['id']}", json={"name": "PETG fast", "settings": {"material": "PETG", "speed": "120"}}).json()
    assert renamed["name"] == "PETG fast" and renamed["settings"] == {"material": "PETG", "speed": "120"}
    assert authed.put(f"/api/profiles/{made['id']}", json={"settings": {}}).status_code == 400
    assert [p["name"] for p in authed.get("/api/profiles").json()["profiles"]] == ["PETG fast"]
    assert authed.delete(f"/api/profiles/{made['id']}").json() == {"status": "deleted"}
    assert authed.delete(f"/api/profiles/{made['id']}").status_code == 404


def test_a_profile_fills_or_replaces_a_models_settings(authed, tidy):
    m = _model(authed)
    try:
        authed.put(f"/api/library/models/{m['id']}/print-settings", json={"material": "PLA", "notes": "orient flat"})
        p = authed.post("/api/profiles", json={"name": "Fine", "settings": {"material": "PETG", "layer_height": "0.12", "supports": "tree"}}).json()
        filled = authed.post(f"/api/profiles/{p['id']}/apply/{m['id']}", json={}).json()["print_settings"]
        assert filled == {"material": "PLA", "notes": "orient flat", "layer_height": "0.12", "supports": "tree"}                      # only the empty boxes
        replaced = authed.post(f"/api/profiles/{p['id']}/apply/{m['id']}", json={"mode": "replace"}).json()["print_settings"]
        assert replaced["material"] == "PETG" and replaced["notes"] == "orient flat"                                              # notes are never touched
        assert authed.get(f"/api/library/models/{m['id']}").json()["print_settings"] == json.dumps(replaced)
        assert authed.post(f"/api/profiles/{p['id']}/apply/{m['id']}", json={"mode": "other"}).status_code == 400
        assert authed.post(f"/api/profiles/{p['id']}/apply/999999", json={}).status_code == 404
        assert authed.post("/api/profiles/999999/apply/1", json={}).status_code == 404
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_models_settings_can_become_a_profile(authed, tidy):
    m = _model(authed)
    try:
        assert authed.post(f"/api/profiles/from-model/{m['id']}", json={"name": "From it"}).status_code == 400            # nothing saved on it yet
        authed.put(f"/api/library/models/{m['id']}/print-settings", json={"material": "ABS", "bed_temp": "100", "notes": "private tip"})
        p = authed.post(f"/api/profiles/from-model/{m['id']}", json={"name": "From it"}).json()
        assert p["settings"] == {"material": "ABS", "bed_temp": "100"}                                                          # notes stay with the model
        assert authed.post(f"/api/profiles/from-model/{m['id']}", json={"name": "From it"}).status_code == 400
        assert authed.post("/api/profiles/from-model/999999", json={"name": "x"}).status_code == 404
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- the status page
def test_the_status_page_is_off_until_switched_on_and_then_needs_its_link(authed, tidy):
    anonymous = TestClient(authed.app)
    assert authed.get("/api/settings/status-page").json() == {"enabled": False, "path": None, "show_files": False}
    assert anonymous.get("/status/anything").status_code == 404 and anonymous.get("/status/anything/data").status_code == 404
    on = authed.post("/api/settings/status-page", json={"enabled": True}).json()
    assert on["enabled"] is True and on["path"].startswith("/status/") and len(on["path"]) > 30
    assert anonymous.get(on["path"] + "x").status_code == 404 and anonymous.get("/status/").status_code in (404, 405)
    page = anonymous.get(on["path"])
    assert page.status_code == 200 and "Print farm" in page.text and page.headers["cache-control"] == "no-store" and "noindex" in page.headers["x-robots-tag"]
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert authed.post("/api/settings/status-page", json={"enabled": True}).json()["path"] == on["path"]               # switching on twice keeps the link
    off = authed.post("/api/settings/status-page", json={"enabled": False}).json()
    assert off["enabled"] is False and anonymous.get(on["path"]).status_code == 404


def test_the_data_shows_only_what_is_safe_and_files_only_when_asked(authed, tidy):
    anonymous = TestClient(authed.app)
    p = authed.post("/api/printers", json={"name": "Wall", "kind": "moonraker", "url": "http://secret-host.local:7125", "api_key": "very-secret-key-1234", "snapshot_url": "http://cam.local/s"}).json()
    q = authed.post("/api/queue", json={"model_id": _model(authed)["id"]}).json()
    held = authed.post("/api/queue", json={"model_id": q["model_id"]}).json()
    authed.patch(f"/api/queue/{held['id']}", json={"held": True})
    link = authed.post("/api/settings/status-page", json={"enabled": True}).json()["path"]
    printwatch.latest[p["id"]] = {"online": True, "state": "printing", "progress": 42.5, "file": "secret_gift.gcode", "nozzle": 215.2, "bed": 60.0, "message": "x", "name": "Wall"}
    try:
        data = anonymous.get(link + "/data").json()
        row = next(r for r in data["printers"] if r["name"] == "Wall")
        assert row == {"name": "Wall", "online": True, "state": "printing", "progress": 42.5, "nozzle": 215.2, "bed": 60.0, "file": None}
        assert data["queue"]["waiting"] >= 2 and data["queue"]["held"] >= 1 and data["updated"].endswith("Z")
        text = json.dumps(data)
        for secret in ("secret-host", "very-secret", "cam.local", "secret_gift", "klipper"):
            assert secret not in text, secret
        shown = authed.post("/api/settings/status-page", json={"show_files": True}).json()
        assert shown["show_files"] is True
        assert next(r for r in anonymous.get(link + "/data").json()["printers"] if r["name"] == "Wall")["file"] == "secret_gift.gcode"
    finally:
        for item in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{item['id']}")
        authed.delete(f"/api/library/models/{q['model_id']}")


def test_a_printer_the_poll_has_not_seen_is_unknown_and_a_new_link_replaces_the_old(authed, tidy):
    anonymous = TestClient(authed.app)
    authed.post("/api/printers", json={"name": "Unseen", "kind": "moonraker", "url": "http://klipper.local:7125"})
    old = authed.post("/api/settings/status-page", json={"enabled": True}).json()["path"]
    row = next(r for r in anonymous.get(old + "/data").json()["printers"] if r["name"] == "Unseen")
    assert row["state"] == "unknown" and row["online"] is False and row["progress"] is None
    new = authed.post("/api/settings/status-page", json={"rotate": True}).json()["path"]
    assert new != old and anonymous.get(old + "/data").status_code == 404 and anonymous.get(new + "/data").status_code == 200


def test_the_link_is_not_in_settings_or_backups_and_only_the_administrator_may_manage_it(authed, tidy):
    from app.main import app
    link = authed.post("/api/settings/status-page", json={"enabled": True}).json()["path"]
    token = link.rsplit("/", 1)[1]
    assert "status_token" not in authed.get("/api/settings").json()
    authed.put("/api/settings", json={"status_token": "chosen-by-me"})                       # reserved: ignored
    assert authed.get("/api/settings/status-page").json()["path"] == link
    backup = authed.get("/api/backup")
    assert backup.status_code == 200 and token.encode() not in backup.content
    assert authed.post("/api/settings/status-page", json={"enabled": "yes"}).status_code == 400
    authed.post("/api/users", json={"username": "stmember", "password": PW, "role": "member"})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "stmember", "password": PW})
        assert member.get("/api/settings/status-page").status_code == 403
        assert member.post("/api/settings/status-page", json={"rotate": True}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


# ---------------------------------------------------------------- the page
def test_the_controls_are_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="status-page-box"' in html
    for needle in ("/api/settings/status-page", "loadStatusPage", "maint-history", "/api/maintenance/log", "ps-profile", "/api/profiles", "apply/${model.id}", "Save as profile"):
        assert needle in js, needle
