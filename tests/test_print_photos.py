"""A picture from the printer's camera is kept with the print when it finishes."""
import io
import uuid

import httpx
import pytest
from PIL import Image

from app import printers as printing, printwatch
from test_printers import FakePrinter

PW = "a long enough password"


def _jpeg(color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(out, "JPEG")
    return out.getvalue()


class Cameras(FakePrinter):
    """A printer plus a camera host that serves a still picture."""

    def __init__(self):
        super().__init__()
        self.camera_hits = []
        self.camera_status = 200
        self.camera_type = "image/jpeg"
        self.camera_body = _jpeg()

    def __call__(self, request):
        if request.url.host == "camera.local":
            self.camera_hits.append((request.url.path, request.url.query, request.headers.get("x-api-key")))
            if request.url.path == "/redirect":
                return httpx.Response(302, headers={"location": "http://elsewhere.example/x.jpg"})
            if request.url.path == "/slow":
                raise httpx.ReadTimeout("slow")
            return httpx.Response(self.camera_status, content=self.camera_body, headers={"content-type": self.camera_type})
        return super().__call__(request)


@pytest.fixture()
def cams(monkeypatch, authed):
    f = Cameras()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    printwatch.reset()
    yield f
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")


@pytest.fixture()
def model(authed):
    n = 40 + uuid.uuid4().int % 150
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()
    m = authed.post("/api/library/import", files={"file": (f"ph_{uuid.uuid4().hex[:6]}.stl", stl, "application/octet-stream")}).json()
    yield m
    for q in authed.get("/api/queue").json():
        if q["model_id"] == m["id"]:
            authed.delete(f"/api/queue/{q['id']}")
    authed.delete(f"/api/library/models/{m['id']}")


def _printer(c, **extra):
    r = c.post("/api/printers", json={"name": "Cam printer", "kind": "moonraker", "url": "http://klipper.local:7125", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _finish_a_print(printer, model, fake):
    from sqlmodel import Session
    from app.db import engine
    from app.models import PrinterJob
    with Session(engine) as s:
        s.add(PrinterJob(printer_id=printer["id"], filename="benchy.gcode", model_id=model["id"], started=True))
        s.commit()
        fake.moonraker_state = "printing"
        printwatch.poll(s)
        fake.moonraker_state = "complete"
        printwatch.poll(s)


def test_the_camera_address_is_saved_validated_and_can_be_cleared(authed, cams):
    p = _printer(authed, snapshot_url="camera.local/snapshot?action=snapshot")
    assert p["snapshot_url"] == "http://camera.local/snapshot?action=snapshot"
    assert authed.patch(f"/api/printers/{p['id']}", json={"snapshot_url": ""}).json()["snapshot_url"] is None
    for bad in ("ftp://camera.local/x", "http://user:pw@camera.local/x", "http://camera.local/x#frag", 12):
        assert authed.patch(f"/api/printers/{p['id']}", json={"snapshot_url": bad}).status_code == 400
    assert authed.get("/api/printers").json()["printers"][0]["snapshot_url"] is None


def test_try_the_camera_shows_a_picture_and_explains_failures(authed, cams):
    p = _printer(authed)
    assert authed.post(f"/api/printers/{p['id']}/snapshot").status_code == 400                    # no address yet
    authed.patch(f"/api/printers/{p['id']}", json={"snapshot_url": "http://camera.local/snapshot"})
    r = authed.post(f"/api/printers/{p['id']}/snapshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(r.content)).size == (640, 480)
    cams.camera_type = "text/html"
    assert "did not give a picture" in authed.post(f"/api/printers/{p['id']}/snapshot").json()["detail"]
    cams.camera_type, cams.camera_status = "image/jpeg", 404
    assert "error (404)" in authed.post(f"/api/printers/{p['id']}/snapshot").json()["detail"]
    cams.camera_status, cams.camera_body = 200, b"not an image at all"
    assert authed.post(f"/api/printers/{p['id']}/snapshot").status_code == 502
    authed.patch(f"/api/printers/{p['id']}", json={"snapshot_url": "http://camera.local/redirect"})
    assert "redirects" in authed.post(f"/api/printers/{p['id']}/snapshot").json()["detail"]
    authed.patch(f"/api/printers/{p['id']}", json={"snapshot_url": "http://camera.local/slow"})
    assert "did not answer" in authed.post(f"/api/printers/{p['id']}/snapshot").json()["detail"]


def test_the_printers_api_key_is_never_sent_to_the_camera(authed, cams):
    p = _printer(authed, kind="octoprint", url="http://octopi.local", api_key="top-secret-key", snapshot_url="http://camera.local/snapshot")
    authed.post(f"/api/printers/{p['id']}/snapshot")
    assert cams.camera_hits and all(key is None for _, _, key in cams.camera_hits)


def test_an_oversized_picture_is_refused(authed, cams, monkeypatch):
    p = _printer(authed, snapshot_url="http://camera.local/snapshot")
    monkeypatch.setattr(printing, "MAX_SNAPSHOT_BYTES", 100)
    assert "larger than 8 MB" in authed.post(f"/api/printers/{p['id']}/snapshot").json()["detail"]


def test_a_finished_print_gets_the_cameras_picture_as_its_photo(authed, cams, model):
    p = _printer(authed, snapshot_url="http://camera.local/snapshot")
    _finish_a_print(p, model, cams)
    log = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]
    assert log["has_photo"] is True
    photo = authed.get(f"/api/prints/{log['id']}/photo")
    assert photo.status_code == 200 and Image.open(io.BytesIO(photo.content)).format == "JPEG"
    assert len(cams.camera_hits) == 1


def test_a_queue_entry_that_finishes_gets_the_photo_too(authed, cams, model):
    p = _printer(authed, snapshot_url="http://camera.local/snapshot")
    authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 30, "printer_id": p["id"]})
    _finish_a_print(p, model, cams)
    logs = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"]
    assert len(logs) == 1 and logs[0]["source"] == "queue" and logs[0]["has_photo"] is True


def test_no_camera_address_means_no_photo_and_no_request(authed, cams, model):
    p = _printer(authed)
    _finish_a_print(p, model, cams)
    assert authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]["has_photo"] is False
    assert cams.camera_hits == []


@pytest.mark.parametrize("failure", ["status", "html", "garbage", "timeout"])
def test_a_broken_camera_never_stops_the_print_being_recorded(authed, cams, model, failure):
    path = "/slow" if failure == "timeout" else "/snapshot"
    p = _printer(authed, snapshot_url=f"http://camera.local{path}")
    if failure == "status":
        cams.camera_status = 500
    elif failure == "html":
        cams.camera_type = "text/html"
    elif failure == "garbage":
        cams.camera_body = b"not a picture"
    _finish_a_print(p, model, cams)
    log = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]
    assert log["source"] == "printer" and log["measured"] is True and log["has_photo"] is False


def test_a_stopped_print_logs_nothing_and_fetches_nothing(authed, cams, model):
    from sqlmodel import Session
    from app.db import engine
    p = _printer(authed, snapshot_url="http://camera.local/snapshot")
    with Session(engine) as s:
        cams.moonraker_state = "printing"
        printwatch.poll(s)
        cams.moonraker_state = "error"
        printwatch.poll(s)
    assert cams.camera_hits == []


def test_an_existing_photo_is_not_replaced(authed, cams, model):
    """If a log already has a photo (someone added one just before), the camera does not overwrite it."""
    p = _printer(authed, snapshot_url="http://camera.local/snapshot")
    _finish_a_print(p, model, cams)
    log_id = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]["id"]
    first = authed.get(f"/api/prints/{log_id}/photo").content
    cams.camera_body = _jpeg((10, 10, 250))
    from sqlmodel import Session
    from app.db import engine
    with Session(engine) as s:
        from app.models import Printer
        printwatch._photograph(s, s.get(Printer, p["id"]), model["id"])
    assert authed.get(f"/api/prints/{log_id}/photo").content == first
    assert len(cams.camera_hits) == 1


def test_only_the_administrator_can_use_the_camera_check(authed, cams):
    from starlette.testclient import TestClient
    from app.main import app
    p = _printer(authed, snapshot_url="http://camera.local/snapshot")
    authed.post("/api/users", json={"username": "photomember", "password": PW})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "photomember", "password": PW})
        assert member.post(f"/api/printers/{p['id']}/snapshot").status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
