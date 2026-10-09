"""Camera watching, mesh repair, phone sharing, creator pages, drying reminders and the storage overview."""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session
from starlette.testclient import TestClient

from app import drying, failure_watch, printers as printing, printwatch
from app.db import engine
from app.models import Filament, SpoolSlot

PW = "a long enough password"


def _cube(n, holes=0):
    """An ASCII STL cube (n mm) with `holes` of its twelve triangles left out, so it has an opening."""
    v = [(0, 0, 0), (n, 0, 0), (n, n, 0), (0, n, 0), (0, 0, n), (n, 0, n), (n, n, n), (0, n, n)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    out = ["solid c"]
    for a, b, c in tris[holes:]:
        out += ["facet normal 0 0 0", " outer loop"] + [f"  vertex {x} {y} {z}" for x, y, z in (v[a], v[b], v[c])] + [" endloop", "endfacet"]
    out.append("endsolid c")
    return "\n".join(out).encode()


def _import(c, content, name=None):
    name = name or f"sc_{uuid.uuid4().hex[:6]}.stl"
    r = c.post("/api/library/import", files={"file": (name, content, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def _salt():
    return 30 + uuid.uuid4().int % 400


@pytest.fixture()
def clean(authed):
    yield
    failure_watch.reset()
    printwatch.reset()
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")
    for f in authed.get("/api/filament").json():
        authed.delete(f"/api/filament/{f['id']}")
    with Session(engine) as s:
        for slot in s.query(SpoolSlot).all():
            s.delete(slot)
        s.commit()


# ---------------------------------------------------------------- watching a camera
def _printer(c, **extra):
    r = c.post("/api/printers", json={"name": "Cam", "kind": "moonraker", "url": "http://klipper.local:7125", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_watching_needs_a_camera_address_and_is_off_until_switched_on(authed, clean):
    p = _printer(authed)
    assert p["watch_failures"] is False
    assert authed.patch(f"/api/printers/{p['id']}", json={"watch_failures": True}).status_code == 400
    assert authed.patch(f"/api/printers/{p['id']}", json={"watch_failures": "yes"}).status_code == 400
    ok = authed.patch(f"/api/printers/{p['id']}", json={"snapshot_url": "http://cam.local/snap.jpg", "watch_failures": True})
    assert ok.status_code == 200 and ok.json()["watch_failures"] is True


def test_two_bad_looks_in_a_row_warn_once_and_only_while_printing(authed, clean, monkeypatch):
    p = _printer(authed, snapshot_url="http://cam.local/snap.jpg")
    authed.patch(f"/api/printers/{p['id']}", json={"watch_failures": True})
    told, answers = [], iter([])
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: b"jpeg")
    monkeypatch.setattr(failure_watch, "verdict", lambda session, picture: next(answers))
    monkeypatch.setattr(failure_watch, "notify_event", lambda session, event, title, message: told.append((event, title, message)))
    failure_watch.reset()

    def look(*results):
        nonlocal answers
        answers = iter(results)
        out = []
        for _ in results:
            with Session(engine) as s:
                out.append(failure_watch.scheduled(s))
        return out

    printwatch.latest[p["id"]] = {"state": "idle", "file": None, "progress": None}
    assert look({"failed": True, "reason": "x"}) == [[]]                                                # not printing: no picture is taken
    assert told == []
    printwatch.latest[p["id"]] = {"state": "printing", "file": "benchy.gcode", "progress": 41}
    first, second, third, fourth = look({"failed": True, "reason": "spaghetti"}, {"failed": True, "reason": "spaghetti"}, {"failed": True, "reason": "spaghetti"}, {"failed": False, "reason": ""})
    assert (first, second, third, fourth) == ([], ["Cam"], [], [])                                       # one bad look is not enough, and it is only said once
    assert len(told) == 1 and told[0][0] == "failure_suspected" and "spaghetti" in told[0][2] and "41%" in told[0][2] and "benchy" in told[0][2]
    a, b = look({"failed": True, "reason": "again"}, {"failed": True, "reason": "again"})
    assert b == [] and len(told) == 1                                                                    # told already for this print
    printwatch.latest[p["id"]] = {"state": "idle"}
    with Session(engine) as s:
        failure_watch.scheduled(s)                                                                       # the print ended: a clean slate
    printwatch.latest[p["id"]] = {"state": "printing", "file": "next.gcode", "progress": 3}
    c, d = look({"failed": True, "reason": "blob"}, {"failed": True, "reason": "blob"})
    assert d == ["Cam"] and len(told) == 2


def test_an_unwatched_printer_is_never_looked_at(authed, clean, monkeypatch):
    p = _printer(authed, snapshot_url="http://cam.local/snap.jpg")
    calls = []
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: calls.append(url) or b"x")
    printwatch.latest[p["id"]] = {"state": "printing", "file": "a", "progress": 1}
    with Session(engine) as s:
        assert failure_watch.scheduled(s) == [] and calls == []


def test_the_test_button_reports_the_models_answer_and_explains_failures(authed, clean, monkeypatch):
    p = _printer(authed, snapshot_url="http://cam.local/snap.jpg")
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: b"jpeg")
    monkeypatch.setattr(failure_watch, "verdict", lambda session, picture: {"failed": False, "reason": "looks fine"})
    assert authed.post(f"/api/printers/{p['id']}/watch-test").json() == {"failed": False, "reason": "looks fine"}

    def broken(session, picture):
        raise failure_watch.WatchError("The vision model did not answer (ConnectError)")
    monkeypatch.setattr(failure_watch, "verdict", broken)
    r = authed.post(f"/api/printers/{p['id']}/watch-test")
    assert r.status_code == 502 and "vision model" in r.json()["detail"]
    q = _printer(authed, name="NoCam")
    assert authed.post(f"/api/printers/{q['id']}/watch-test").status_code == 502


def test_the_vision_model_answer_is_read_carefully(authed, clean, monkeypatch):
    import app.ai.http_utils as http
    seen = {}

    def fake(url, payload, timeout, **kw):
        seen["payload"] = payload
        return seen["reply"]
    monkeypatch.setattr(http, "post_json_bounded", fake)
    authed.put("/api/settings", json={"ai_mode": "local", "ollama_host": "http://ollama.local:11434", "ollama_vision_model": "llava"})
    try:
        with Session(engine) as s:
            seen["reply"] = {"response": '{"failed": true, "reason": "strings in the air"}'}
            assert failure_watch.verdict(s, b"jpg") == {"failed": True, "reason": "strings in the air"}
            assert seen["payload"]["model"] == "llava" and seen["payload"]["images"] and seen["payload"]["stream"] is False
            seen["reply"] = {"response": '{"failed": "true"}'}                                          # only a real true counts
            assert failure_watch.verdict(s, b"jpg")["failed"] is False
            seen["reply"] = {"response": "not json"}
            with pytest.raises(failure_watch.WatchError):
                failure_watch.verdict(s, b"jpg")
        authed.put("/api/settings", json={"ai_mode": "off"})
        with Session(engine) as s, pytest.raises(failure_watch.WatchError):
            failure_watch.verdict(s, b"jpg")
    finally:
        authed.put("/api/settings", json={"ai_mode": "local"})


def test_the_scheduler_has_the_new_jobs():
    from app import scheduler
    assert {"watch", "drying"} <= set(scheduler._jobs()) and scheduler.INTERVALS["watch"] == 120


# ---------------------------------------------------------------- mesh health and repair
def test_a_sound_mesh_is_reported_healthy(authed):
    m = _import(authed, _cube(_salt()))
    try:
        h = authed.get(f"/api/library/models/{m['id']}/health").json()
        assert h["ok"] is True and h["watertight"] is True and h["issues"] == [] and h["fixable"] is False and h["faces"] == 12
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_mesh_with_a_hole_is_found_and_repaired_into_a_new_version(authed):
    m = _import(authed, _cube(_salt(), holes=2))                                                         # one square face missing
    copy_id = None
    try:
        h = authed.get(f"/api/library/models/{m['id']}/health").json()
        assert h["ok"] is False and h["watertight"] is False and h["open_edges"] >= 4 and h["fixable"] is True
        assert any("open edge" in i for i in h["issues"])
        r = authed.post(f"/api/library/models/{m['id']}/repair")
        assert r.status_code == 200, r.text
        out = r.json()
        copy_id = out["model"]["id"]
        assert out["before"]["watertight"] is False and out["after"]["watertight"] is True and out["after"]["ok"] is True
        assert copy_id != m["id"] and "repaired" in out["model"]["filename"]
        assert authed.get(f"/api/library/models/{copy_id}/health").json()["ok"] is True
        original = authed.get(f"/api/library/models/{m['id']}").json()
        repaired = authed.get(f"/api/library/models/{copy_id}").json()
        assert original["family_id"] and original["family_id"] == repaired["family_id"]
        assert repaired["version_label"] == "repaired" and original["version_label"] == "original"
        assert authed.get(f"/api/library/models/{m['id']}/health").json()["watertight"] is False         # the original is untouched
    finally:
        for i in (copy_id, m["id"]):
            if i:
                authed.delete(f"/api/library/models/{i}")


def test_health_and_repair_refuse_what_they_cannot_do(authed):
    assert authed.get("/api/library/models/999999/health").status_code == 404
    assert authed.post("/api/library/models/999999/repair").status_code == 404
    m = _import(authed, b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n" + uuid.uuid4().hex.encode(), name=f"p_{uuid.uuid4().hex[:6]}.step")
    try:
        assert authed.get(f"/api/library/models/{m['id']}/health").status_code == 400
        assert authed.post(f"/api/library/models/{m['id']}/repair").status_code == 400
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- sharing from a phone
def test_the_app_registers_as_a_share_target(authed):
    m = authed.get("/manifest.webmanifest").json()
    target = m["share_target"]
    assert target["action"] == "/share-target" and target["method"] == "POST" and target["enctype"] == "multipart/form-data"
    assert target["params"]["files"][0]["name"] == "file" and ".stl" in target["params"]["files"][0]["accept"]


def test_a_shared_file_lands_in_the_library_and_opens_it(authed):
    name = f"shared_{uuid.uuid4().hex[:6]}.stl"
    r = authed.post("/share-target", files=[("file", (name, _cube(_salt()), "application/octet-stream"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#/library"
    found = [x for x in authed.get("/api/library/models", params={"q": name[:-4]}).json() if x["filename"].startswith("shared_")]
    assert found
    for x in found:
        authed.delete(f"/api/library/models/{x['id']}")


def test_a_shared_zip_and_a_bad_file_are_handled(authed):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"in_{uuid.uuid4().hex[:6]}.stl", _cube(_salt()))
    r = authed.post("/share-target", files=[("file", ("pack.zip", buf.getvalue(), "application/zip"))], follow_redirects=False)
    assert r.status_code == 303
    bad = authed.post("/share-target", files=[("file", ("<b>x</b>.exe", b"MZ", "application/octet-stream"))], follow_redirects=False)
    assert bad.status_code == 400 and "<b>" not in bad.text and "Nothing was added" in bad.text           # the name is escaped, nothing is added
    for x in authed.get("/api/library/models", params={"q": "in_"}).json():
        authed.delete(f"/api/library/models/{x['id']}")


def test_a_read_only_account_cannot_share_a_file_in(authed):
    from app.main import app
    authed.post("/api/users", json={"username": "scviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "scviewer", "password": PW})
        r = viewer.post("/share-target", files=[("file", ("a.stl", _cube(_salt()), "application/octet-stream"))], follow_redirects=False)
        assert r.status_code == 403
        assert TestClient(app).post("/share-target", files=[("file", ("a.stl", b"x", "application/octet-stream"))], follow_redirects=False).status_code in (401, 403)
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


# ---------------------------------------------------------------- creators
def test_creators_are_listed_with_their_models_and_prints(authed):
    who = f"Designer {uuid.uuid4().hex[:6]}"
    a = _import(authed, _cube(_salt()))
    b = _import(authed, _cube(_salt()))
    try:
        for m, prov in ((a, "printables"), (b, "makerworld")):
            authed.patch(f"/api/library/models/{m['id']}", json={"designer": who, "license": "CC-BY"})
        listing = authed.get("/api/creators", params={"q": who.lower()}).json()["creators"]
        assert [c["name"] for c in listing] == [who] and listing[0]["models"] == 2
        detail = authed.get("/api/creators/detail", params={"name": who}).json()
        assert detail["total"] == 2 and {x["id"] for x in detail["models"]} == {a["id"], b["id"]} and detail["licenses"] == ["CC-BY"]
        assert authed.get("/api/creators/detail", params={"name": "Nobody At All"}).status_code == 404
        assert authed.get("/api/creators/detail", params={"name": "  "}).status_code == 400
    finally:
        for m in (a, b):
            authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- drying
def _spool(c, material="PLA", **extra):
    r = c.post("/api/filament", json={"material": material, "brand": "Dry", "color": "grey", "spool_weight_g": 1000, "remaining_g": 800, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_each_material_has_its_own_rule_of_thumb():
    assert drying.every_days("PLA") == 90 and drying.every_days("pa-cf") == 7 and drying.every_days("Nylon") == 7
    assert drying.every_days("TPU") == 30 and drying.every_days("Mystery") == drying.DEFAULT_DAYS and drying.every_days(None) == drying.DEFAULT_DAYS


def test_a_spool_without_dates_has_nothing_to_say_and_dates_give_a_status(authed, clean):
    s = _spool(authed)
    assert str(s["id"]) not in authed.get("/api/filament/drying/overview").json()
    now = authed.post(f"/api/filament/{s['id']}/opened").json()
    assert now["status"] == "ok" and now["days_since"] == 0 and now["every_days"] == 90
    for days, expected in ((50, "ok"), (80, "soon"), (91, "due")):
        with Session(engine) as db:
            row = db.get(Filament, s["id"])
            row.opened_at = datetime.utcnow() - timedelta(days=days)
            db.add(row)
            db.commit()
        assert authed.get("/api/filament/drying/overview").json()[str(s["id"])]["status"] == expected
    dried = authed.post(f"/api/filament/{s['id']}/dried").json()
    assert dried["status"] == "ok" and dried["days_since"] == 0                                           # drying restarts the clock
    assert authed.post("/api/filament/999999/dried").status_code == 404
    assert authed.post("/api/filament/999999/opened").status_code == 404


def test_only_a_due_spool_in_a_printer_is_reminded_about_once(authed, clean, monkeypatch):
    told = []
    monkeypatch.setattr(drying, "notify_event", lambda session, event, title, message: told.append((event, message)))
    wet = _spool(authed, "PA", color="wet")
    shelf = _spool(authed, "PA", color="shelf")
    fresh = _spool(authed, "PA", color="fresh")
    old = datetime.utcnow() - timedelta(days=30)
    with Session(engine) as db:
        for sid, when in ((wet["id"], old), (shelf["id"], old), (fresh["id"], datetime.utcnow())):
            row = db.get(Filament, sid)
            row.opened_at = when
            db.add(row)
        db.add(SpoolSlot(printer_id=1, slot=1, filament_id=wet["id"]))
        db.add(SpoolSlot(printer_id=1, slot=2, filament_id=fresh["id"]))
        db.commit()
    with Session(engine) as db:
        assert len(drying.check_and_notify(db)) == 1
    assert len(told) == 1 and told[0][0] == "dry_due" and "wet" in told[0][1] and "shelf" not in told[0][1] and "fresh" not in told[0][1]
    with Session(engine) as db:
        assert drying.check_and_notify(db) == []                                                         # not again
    authed.post(f"/api/filament/{wet['id']}/dried")
    with Session(engine) as db:
        assert drying.check_and_notify(db) == []                                                         # dried: nothing due, and the memory resets
        row = db.get(Filament, wet["id"])
        row.dried_at = old
        db.add(row)
        db.commit()
    with Session(engine) as db:
        assert len(drying.check_and_notify(db)) == 1                                                     # due again later: told again
    assert len(told) == 2


def test_the_new_notifications_can_be_switched_off_like_any_other(authed):
    from app.notify import EVENTS
    assert "failure_suspected" in EVENTS and "dry_due" in EVENTS


# ---------------------------------------------------------------- storage
def test_the_storage_overview_adds_things_up(authed):
    m = _import(authed, _cube(_salt()))
    try:
        s = authed.get("/api/storage").json()
        assert s["models"] >= 1 and s["library_bytes"] >= m["size_bytes"]
        stl = next(t for t in s["by_type"] if t["extension"] == ".stl")
        assert stl["models"] >= 1 and stl["bytes"] >= m["size_bytes"]
        assert {f["name"] for f in s["folders"]} >= {"thumbnails", "backups"}
        assert s["biggest"] and all({"id", "filename", "bytes"} <= set(b) for b in s["biggest"])
        assert s["biggest"] == sorted(s["biggest"], key=lambda b: -(b["bytes"] or 0))
        assert set(s["library_disk"]) == {"total", "used", "free"} and s["duplicates"]["models"] >= 0
        assert s["by_type"] == sorted(s["by_type"], key=lambda t: -t["bytes"])
    finally:
        authed.delete(f"/api/library/models/{m['id']}")
