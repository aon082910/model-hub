"""Queue entries assigned to printers: totals per printer, sending the next job, and finishing the right entry."""
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from app import printers as printing
from app import printwatch
from test_printers import FakePrinter

PW = "a long enough password"
GCODE = b"G28\n; layer_height = 0.2\n; estimated printing time (normal mode) = 40m\n"


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def farm(authed, monkeypatch):
    fake = FakePrinter()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(fake), timeout=timeout))
    printwatch.reset()
    a = authed.post("/api/printers", json={"name": "Alpha", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    b = authed.post("/api/printers", json={"name": "Bravo", "kind": "moonraker", "url": "http://klipper.local:7126"}).json()
    m = authed.post("/api/library/import", files={"file": (f"pf_{uuid.uuid4().hex[:6]}.stl", _stl(40 + uuid.uuid4().int % 90), "application/octet-stream")}).json()
    queued = []
    yield fake, a, b, m, queued
    for q in queued:
        authed.delete(f"/api/queue/{q}")
    for q in authed.get("/api/queue").json():
        if q["model_id"] == m["id"]:
            authed.delete(f"/api/queue/{q['id']}")
    authed.delete(f"/api/library/models/{m['id']}")
    for p in (a, b):
        authed.delete(f"/api/printers/{p['id']}")


def _queue(c, farm_fixture, **fields):
    fake, a, b, m, queued = farm_fixture
    r = c.post("/api/queue", json={"model_id": m["id"], **fields})
    assert r.status_code == 200, r.text
    queued.append(r.json()["id"])
    return r.json()


def _keep(c, m, data=GCODE, name="plate.gcode"):
    r = c.post("/api/print-files", data={"model_id": str(m["id"])}, files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_job_can_be_assigned_to_a_printer_and_reassigned(authed, farm):
    fake, a, b, m, _ = farm
    item = _queue(authed, farm, printer_id=a["id"], estimated_minutes=90)
    assert item["printer_id"] == a["id"]
    assert authed.patch(f"/api/queue/{item['id']}", json={"printer_id": b["id"]}).json()["printer_id"] == b["id"]
    assert authed.patch(f"/api/queue/{item['id']}", json={"printer_id": None}).json()["printer_id"] is None
    assert authed.patch(f"/api/queue/{item['id']}", json={"printer_id": 987654}).status_code == 400
    assert authed.post("/api/queue", json={"model_id": m["id"], "printer_id": 987654}).status_code == 400


def test_the_summary_adds_up_each_printers_waiting_time(authed, farm):
    fake, a, b, m, _ = farm
    _queue(authed, farm, printer_id=a["id"], estimated_minutes=90)
    _queue(authed, farm, printer_id=a["id"], estimated_minutes=30)
    running = _queue(authed, farm, printer_id=a["id"], estimated_minutes=10)
    authed.patch(f"/api/queue/{running['id']}", json={"status": "printing"})
    done = _queue(authed, farm, printer_id=a["id"], estimated_minutes=500)
    authed.patch(f"/api/queue/{done['id']}", json={"status": "done"})                   # finished jobs are not waiting time
    _queue(authed, farm, printer_id=b["id"])                                              # no estimate
    _queue(authed, farm)
    rows = {r["printer"]: r for r in authed.get("/api/queue/summary").json()["printers"]}
    assert rows["Alpha"]["jobs"] == 3 and rows["Alpha"]["minutes"] == 130 and rows["Alpha"]["printing"] == 1
    assert rows["Bravo"]["jobs"] == 1 and rows["Bravo"]["without_estimate"] == 1
    assert rows["Not assigned"]["jobs"] >= 1


def test_sending_a_queue_entry_uses_the_models_newest_kept_gcode(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m, b"G28\n; old\n", "old.gcode")
    newest = _keep(authed, m, GCODE, "newest plate.gcode")
    item = _queue(authed, farm, printer_id=a["id"])
    fake.moonraker_state = "standby"
    r = authed.post(f"/api/queue/{item['id']}/send", json={"start": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"filename": "newest_plate.gcode", "started": False, "status": "queued"}
    assert b"estimated printing time" in fake.uploads[0][1]
    from sqlmodel import Session, select
    from app.db import engine
    from app.models import PrinterJob
    with Session(engine) as s:
        job = s.exec(select(PrinterJob).where(PrinterJob.print_file_id == newest["id"])).first()
        assert job and job.model_id == m["id"] and job.printer_id == a["id"]


def test_starting_marks_the_entry_as_printing_and_it_cannot_be_sent_twice(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    item = _queue(authed, farm, printer_id=a["id"])
    fake.moonraker_state = "standby"
    r = authed.post(f"/api/queue/{item['id']}/send", json={"start": True})
    assert r.status_code == 200 and r.json()["status"] == "printing" and r.json()["started"] is True
    again = authed.post(f"/api/queue/{item['id']}/send", json={"start": True})
    assert again.status_code == 409 and "already printing" in again.json()["detail"]


def test_a_busy_or_unreachable_printer_is_not_sent_to(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    item = _queue(authed, farm, printer_id=a["id"])
    fake.moonraker_state = "printing"
    r = authed.post(f"/api/queue/{item['id']}/send", json={})
    assert r.status_code == 409 and "busy" in r.json()["detail"] and fake.uploads == []
    fake.fail = httpx.ConnectError("down")
    assert authed.post(f"/api/queue/{item['id']}/send", json={}).status_code == 502
    assert fake.uploads == []


def test_sending_needs_a_printer_and_a_kept_gcode_file(authed, farm):
    fake, a, b, m, _ = farm
    item = _queue(authed, farm)
    assert authed.post(f"/api/queue/{item['id']}/send", json={}).status_code == 400
    authed.patch(f"/api/queue/{item['id']}", json={"printer_id": a["id"]})
    r = authed.post(f"/api/queue/{item['id']}/send", json={})
    assert r.status_code == 400 and "sliced G-code" in r.json()["detail"]
    _keep(authed, m, b"not gcode but a 3mf", "plate.3mf")                               # a 3MF cannot be sent
    assert authed.post(f"/api/queue/{item['id']}/send", json={}).status_code == 400
    assert authed.post("/api/queue/987654/send", json={}).status_code == 404
    assert fake.uploads == []


def test_only_the_administrator_can_send_to_a_printer(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    item = _queue(authed, farm, printer_id=a["id"])
    authed.post("/api/users", json={"username": "farmmember", "password": PW, "role": "member"})
    from app.main import app
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "farmmember", "password": PW})
        assert member.get("/api/queue/summary").status_code == 200
        r = member.post(f"/api/queue/{item['id']}/send", json={"start": True})
        assert r.status_code == 403 and fake.uploads == []
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


def test_a_finished_print_completes_the_entry_meant_for_that_printer(authed, farm):
    fake, a, b, m, _ = farm
    for_bravo = _queue(authed, farm, printer_id=b["id"])
    for_alpha = _queue(authed, farm, printer_id=a["id"])
    from sqlmodel import Session
    from app.db import engine
    from app.models import PrinterJob
    with Session(engine) as s:
        s.add(PrinterJob(printer_id=a["id"], filename="benchy.gcode", model_id=m["id"], started=True))
        s.commit()
        fake.moonraker_state = "printing"
        printwatch.poll(s)
        fake.moonraker_state = "complete"
        printwatch.poll(s)
    states = {q["id"]: q["status"] for q in authed.get("/api/queue").json()}
    assert states[for_alpha["id"]] == "done" and states[for_bravo["id"]] == "queued"
