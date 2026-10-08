"""Links to a print's time-lapse video, kept by hand or found on the printer that made it."""
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from app import printers as printing, printwatch
from test_printers import FakePrinter

PW = "a long enough password"


class WithTimelapses(FakePrinter):
    def __init__(self):
        super().__init__()
        self.timelapse_status = 200
        self.octo_status = 200
        self.moon_files = []
        self.octo_files = []

    def __call__(self, request):
        host, path = request.url.host, request.url.path
        if host == "klipper.local" and path == "/server/files/list" and request.url.params.get("root") == "timelapse":
            if self.timelapse_status != 200:
                return httpx.Response(self.timelapse_status)
            return httpx.Response(200, json={"result": self.moon_files})
        if host == "octopi.local" and path == "/api/timelapse":
            if self.octo_status != 200:
                return httpx.Response(self.octo_status)
            return httpx.Response(200, json={"files": self.octo_files})
        return super().__call__(request)


@pytest.fixture()
def fake(monkeypatch, authed):
    f = WithTimelapses()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    printwatch.reset()
    yield f
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")


@pytest.fixture()
def model(authed):
    n = 30 + uuid.uuid4().int % 250
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()
    m = authed.post("/api/library/import", files={"file": (f"tl_{uuid.uuid4().hex[:6]}.stl", stl, "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


def _session():
    from sqlmodel import Session
    from app.db import engine
    return Session(engine)


def _printed_by(authed, model, fake, kind="moonraker", url="http://klipper.local:7125", api_key=None):
    """A print that a printer reported, so the log knows which printer made it."""
    body = {"name": "TL printer", "kind": kind, "url": url}
    if api_key:
        body["api_key"] = api_key
    printer = authed.post("/api/printers", json=body).json()
    from app.models import PrinterJob
    with _session() as s:
        s.add(PrinterJob(printer_id=printer["id"], filename="benchy.gcode", model_id=model["id"], started=True))
        s.commit()
        fake.moonraker_state = "printing"
        printwatch.poll(s)
        fake.moonraker_state = "complete"
        printwatch.poll(s)
    log = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]
    return printer, log


def test_a_link_can_be_added_changed_and_removed(authed, model):
    log = authed.post("/api/prints", json={"model_id": model["id"], "deduct": False, "timelapse_url": "https://example.com/v/1.mp4"}).json()
    assert log["timelapse_url"] == "https://example.com/v/1.mp4"
    edited = authed.patch(f"/api/prints/{log['id']}", json={"timelapse_url": " http://192.168.1.5/tl/2.mp4 "}).json()
    assert edited["timelapse_url"] == "http://192.168.1.5/tl/2.mp4"
    assert authed.patch(f"/api/prints/{log['id']}", json={"timelapse_url": ""}).json()["timelapse_url"] is None


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://x/y.mp4", "http://user:pw@host/v.mp4", "//host/v.mp4", "not a link", "http://" + "a" * 600, 5])
def test_only_web_addresses_without_passwords_are_kept(authed, model, bad):
    log = authed.post("/api/prints", json={"model_id": model["id"], "deduct": False}).json()
    assert authed.patch(f"/api/prints/{log['id']}", json={"timelapse_url": bad}).status_code == 400
    assert authed.post("/api/prints", json={"model_id": model["id"], "timelapse_url": bad}).status_code == 400


def test_a_print_remembers_which_printer_reported_it(authed, model, fake):
    printer, log = _printed_by(authed, model, fake)
    assert log["printer_id"] == printer["id"] and log["source"] == "printer"


def test_klipper_time_lapses_are_offered_closest_to_the_print_first_and_only_videos(authed, model, fake):
    import time
    now = time.time()
    fake.moon_files = [{"path": "old_benchy.mp4", "modified": now - 86400 * 9, "size": 10},
                       {"path": "frames.zip", "modified": now, "size": 5},
                       {"path": "benchy_new.mp4", "modified": now + 30, "size": 20},
                       {"path": "sub/dir clip.mp4", "modified": now + 600, "size": 30}]
    printer, log = _printed_by(authed, model, fake)
    r = authed.get(f"/api/prints/{log['id']}/timelapse-candidates")
    assert r.status_code == 200 and r.json()["printer"] == "TL printer"
    names = [f["name"] for f in r.json()["files"]]
    assert names == ["benchy_new.mp4", "sub/dir clip.mp4", "old_benchy.mp4"]
    urls = {f["name"]: f["url"] for f in r.json()["files"]}
    assert urls["benchy_new.mp4"] == "http://klipper.local:7125/server/files/timelapse/benchy_new.mp4"
    assert urls["sub/dir clip.mp4"].endswith("/server/files/timelapse/sub/dir%20clip.mp4") or "dir%20clip" in urls["sub/dir clip.mp4"]


def test_octoprint_time_lapses_are_listed_too(authed, model, fake):
    fake.octo_files = [{"name": "one.mp4", "bytes": 1000, "url": "/downloads/timelapse/one.mp4"},
                       {"name": "bad.mp4", "url": "http://elsewhere.example/bad.mp4"},          # not on the printer: never offered
                       {"name": "frames.tar", "url": "/downloads/timelapse/frames.tar"}]
    printer = authed.post("/api/printers", json={"name": "Octo", "kind": "octoprint", "url": "http://octopi.local", "api_key": "k" * 20}).json()
    log = authed.post("/api/prints", json={"model_id": model["id"], "deduct": False}).json()
    from app.models import PrintLog
    with _session() as s:
        row = s.get(PrintLog, log["id"])
        row.printer_id = printer["id"]
        s.add(row)
        s.commit()
    files = authed.get(f"/api/prints/{log['id']}/timelapse-candidates").json()["files"]
    assert [(f["name"], f["url"]) for f in files] == [("one.mp4", "http://octopi.local/downloads/timelapse/one.mp4")]


def test_no_printer_a_printer_without_the_feature_and_a_broken_printer(authed, model, fake):
    manual = authed.post("/api/prints", json={"model_id": model["id"], "deduct": False}).json()
    assert authed.get(f"/api/prints/{manual['id']}/timelapse-candidates").status_code == 400
    assert authed.get("/api/prints/987654/timelapse-candidates").status_code == 404
    printer, log = _printed_by(authed, model, fake)
    fake.timelapse_status = 404                                                    # the plugin is not installed
    assert authed.get(f"/api/prints/{log['id']}/timelapse-candidates").json()["files"] == []
    fake.timelapse_status = 500
    assert authed.get(f"/api/prints/{log['id']}/timelapse-candidates").status_code == 502
    fake.fail = httpx.ConnectError("down")
    assert authed.get(f"/api/prints/{log['id']}/timelapse-candidates").status_code == 502


def test_the_printers_key_is_not_in_the_answer(authed, model, fake):
    fake.octo_files = [{"name": "one.mp4", "url": "/downloads/timelapse/one.mp4"}]
    printer = authed.post("/api/printers", json={"name": "Octo", "kind": "octoprint", "url": "http://octopi.local", "api_key": "super-secret-key-123"}).json()
    log = authed.post("/api/prints", json={"model_id": model["id"], "deduct": False}).json()
    from app.models import PrintLog
    with _session() as s:
        row = s.get(PrintLog, log["id"])
        row.printer_id = printer["id"]
        s.add(row)
        s.commit()
    assert "super-secret-key-123" not in authed.get(f"/api/prints/{log['id']}/timelapse-candidates").text


def test_only_the_administrator_may_ask_the_printer(authed, model, fake):
    from app.main import app
    printer, log = _printed_by(authed, model, fake)
    authed.post("/api/users", json={"username": "tlmember", "password": PW, "role": "member"})
    authed.post("/api/users", json={"username": "tlviewer", "password": PW, "role": "viewer"})
    try:
        member, viewer = TestClient(app), TestClient(app)
        member.post("/api/auth/login", json={"username": "tlmember", "password": PW})
        viewer.post("/api/auth/login", json={"username": "tlviewer", "password": PW})
        assert member.get(f"/api/prints/{log['id']}/timelapse-candidates").status_code == 403
        assert viewer.get(f"/api/prints/{log['id']}/timelapse-candidates").status_code == 403
        assert member.patch(f"/api/prints/{log['id']}", json={"timelapse_url": "https://example.com/a.mp4"}).status_code == 200      # a link is fine
        assert viewer.patch(f"/api/prints/{log['id']}", json={"timelapse_url": "https://example.com/a.mp4"}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
