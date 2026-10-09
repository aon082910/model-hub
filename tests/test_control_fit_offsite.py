"""Pause, resume and cancel; will it fit on a printer; backups sent off-site."""
import httpx
import pytest
from starlette.testclient import TestClient

from app import offsite, printers as printing, printwatch
from test_printers import FakePrinter

PW = "a long enough password"


class Controllable(FakePrinter):
    def __init__(self):
        super().__init__()
        self.commands = []
        self.control_status = 200

    def __call__(self, request):
        if request.method == "POST" and request.url.path.startswith("/printer/print/"):
            self.commands.append(("moonraker", request.url.path.rsplit("/", 1)[-1]))
            return httpx.Response(self.control_status, json={"result": "ok"})
        if request.method == "POST" and request.url.path == "/api/job":
            self.commands.append(("octoprint", request.read().decode()))
            return httpx.Response(204 if self.control_status == 200 else self.control_status)
        return super().__call__(request)


@pytest.fixture()
def fake(monkeypatch, authed):
    f = Controllable()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    printwatch.reset()
    yield f
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")


def _add(c, **extra):
    r = c.post("/api/printers", json={"name": "Ctl", "kind": "moonraker", "url": "http://klipper.local:7125", **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- control ----------

def test_a_printing_klipper_can_be_paused_and_cancelled_but_not_resumed(authed, fake):
    p = _add(authed)
    fake.moonraker_state = "printing"
    assert authed.post(f"/api/printers/{p['id']}/control", json={"action": "pause"}).json() == {"status": "sent", "action": "pause", "was": "printing"}
    assert authed.post(f"/api/printers/{p['id']}/control", json={"action": "resume"}).status_code == 409
    assert authed.post(f"/api/printers/{p['id']}/control", json={"action": "cancel"}).status_code == 200
    assert fake.commands == [("moonraker", "pause"), ("moonraker", "cancel")]
    fake.moonraker_state = "paused"
    assert authed.post(f"/api/printers/{p['id']}/control", json={"action": "resume"}).status_code == 200
    assert authed.post(f"/api/printers/{p['id']}/control", json={"action": "pause"}).status_code == 409


def test_an_idle_printer_is_not_sent_commands(authed, fake):
    p = _add(authed)
    fake.moonraker_state = "standby"
    for action in ("pause", "resume", "cancel"):
        r = authed.post(f"/api/printers/{p['id']}/control", json={"action": action})
        assert r.status_code == 409 and "standby" in r.json()["detail"]
    assert fake.commands == []


def test_bad_actions_unreachable_printers_and_refusals(authed, fake):
    p = _add(authed)
    assert authed.post(f"/api/printers/{p['id']}/control", json={"action": "explode"}).status_code == 400
    assert authed.post(f"/api/printers/{p['id']}/control", json={}).status_code == 400
    assert authed.post("/api/printers/987654/control", json={"action": "pause"}).status_code == 404
    fake.moonraker_state = "printing"
    fake.control_status = 400
    assert authed.post(f"/api/printers/{p['id']}/control", json={"action": "pause"}).status_code == 502
    fake.fail = httpx.ConnectError("down")
    assert authed.post(f"/api/printers/{p['id']}/control", json={"action": "pause"}).status_code == 502


def test_octoprint_gets_its_own_commands(authed, fake):
    p = _add(authed, name="Octo", kind="octoprint", url="http://octopi.local", api_key="k" * 20)
    authed.post(f"/api/printers/{p['id']}/control", json={"action": "pause"})
    authed.post(f"/api/printers/{p['id']}/control", json={"action": "cancel"})
    assert [c[0] for c in fake.commands] == ["octoprint", "octoprint"]
    assert '"command":"pause"' in fake.commands[0][1].replace(" ", "") and '"action":"pause"' in fake.commands[0][1].replace(" ", "")
    assert '"command":"cancel"' in fake.commands[1][1].replace(" ", "")


def test_a_bambu_printer_gets_the_right_mqtt_command(authed, monkeypatch):
    sent = []
    monkeypatch.setattr(printing, "_bambu_report", lambda *a, **k: {"gcode_state": "RUNNING", "mc_percent": 5})
    monkeypatch.setattr(printing, "_bambu_command", lambda host, serial, code, command, timeout=8: sent.append((host, serial, code, command)))
    p = authed.post("/api/printers", json={"name": "P1S", "kind": "bambu", "url": "192.168.1.9", "serial": "01P00A123456789", "api_key": "12345678"}).json()
    try:
        authed.post(f"/api/printers/{p['id']}/control", json={"action": "pause"})
        authed.post(f"/api/printers/{p['id']}/control", json={"action": "cancel"})
        assert sent == [("192.168.1.9", "01P00A123456789", "12345678", "pause"), ("192.168.1.9", "01P00A123456789", "12345678", "stop")]
    finally:
        authed.delete(f"/api/printers/{p['id']}")


def test_only_the_administrator_may_control_a_printer(authed, fake):
    from app.main import app
    p = _add(authed)
    fake.moonraker_state = "printing"
    authed.post("/api/users", json={"username": "ctlmember", "password": PW, "role": "member"})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "ctlmember", "password": PW})
        assert member.post(f"/api/printers/{p['id']}/control", json={"action": "cancel"}).status_code == 403
        assert fake.commands == []
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


def test_a_cancelled_print_is_then_logged_as_a_failure(authed, fake):
    import uuid
    from sqlmodel import Session
    from app.db import engine
    from app.models import PrinterJob
    n = 30 + uuid.uuid4().int % 250
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()
    m = authed.post("/api/library/import", files={"file": (f"cf_{uuid.uuid4().hex[:6]}.stl", stl, "application/octet-stream")}).json()
    p = _add(authed)
    try:
        with Session(engine) as s:
            s.add(PrinterJob(printer_id=p["id"], filename="benchy.gcode", model_id=m["id"], started=True))
            s.commit()
            fake.moonraker_state = "printing"
            printwatch.poll(s)
            authed.post(f"/api/printers/{p['id']}/control", json={"action": "cancel"})
            fake.moonraker_state = "cancelled"
            assert printwatch.poll(s) == [("Ctl", "stopped")]
        assert authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"][0]["outcome"] == "failed"
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


# ---------- will it fit ----------

def _model(c, n):
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n // 2} {n // 4}\n endloop\nendfacet\nendsolid t\n").encode()
    import uuid
    return c.post("/api/library/import", files={"file": (f"fit_{uuid.uuid4().hex[:6]}.stl", stl + uuid.uuid4().hex.encode(), "application/octet-stream")}).json()


def test_bed_sizes_are_saved_and_checked(authed, fake):
    p = _add(authed, bed_x=256, bed_y=256, bed_z=256)
    assert (p["bed_x"], p["bed_y"], p["bed_z"]) == (256, 256, 256)
    for bad in ("big", -5, 5, 99999, True):
        assert authed.patch(f"/api/printers/{p['id']}", json={"bed_x": bad}).status_code == 400
    assert authed.patch(f"/api/printers/{p['id']}", json={"bed_z": None}).json()["bed_z"] is None


def test_a_model_is_judged_against_each_printers_bed(authed, fake):
    big, small = _add(authed, name="Big", bed_x=300, bed_y=300, bed_z=300), _add(authed, name="Small", bed_x=100, bed_y=100, bed_z=100)
    _add(authed, name="Unknown")
    m = _model(authed, 150)
    try:
        r = authed.get(f"/api/fit/model/{m['id']}").json()
        assert [x["name"] for x in r["fits"]] == ["Big"] and [x["name"] for x in r["too_big"]] == ["Small"]          # the unknown bed is not judged
        authed.post("/api/queue", json={"model_id": m["id"], "printer_id": small["id"]})
        authed.post("/api/queue", json={"model_id": m["id"], "printer_id": big["id"]})
        fits = authed.get("/api/fit/queue").json()
        assert sorted(v["fits"] for v in fits.values()) == [False, True]
        assert authed.get("/api/fit/printers").json()[0]["bed"] in ([300.0, 300.0, 300.0], [100.0, 100.0, 100.0], None)
        ids = [x["id"] for x in authed.get("/api/library/models", params={"fits_printer": small["id"], "limit": 5000}).json()]
        assert m["id"] not in ids
        ids = [x["id"] for x in authed.get("/api/library/models", params={"fits_printer": big["id"], "limit": 5000}).json()]
        assert m["id"] in ids
        assert authed.get("/api/library/models", params={"fits_printer": 987654}).status_code == 400
    finally:
        for q in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{q['id']}")
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_turned_footprint_fits_but_a_too_tall_model_does_not(authed, fake):
    p = _add(authed, name="Narrow", bed_x=100, bed_y=200, bed_z=30)
    m = _model(authed, 180)                                                       # about 180 x 90 x 45
    try:
        assert authed.get(f"/api/fit/model/{m['id']}").json()["too_big"][0]["name"] == "Narrow"           # 45 mm tall on a 30 mm printer
        authed.patch(f"/api/printers/{p['id']}", json={"bed_z": 100})
        assert authed.get(f"/api/fit/model/{m['id']}").json()["fits"][0]["name"] == "Narrow"              # turned a quarter turn: 90 x 180 on 100 x 200
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


# ---------- off-site backups ----------

@pytest.fixture()
def clean_offsite(authed, tmp_path):
    keys = ("offsite_kind", "offsite_path", "offsite_url", "offsite_user", "offsite_password", "offsite_keep", "offsite_last", "offsite_last_error")
    authed.put("/api/settings", json={k: "" for k in keys})
    yield tmp_path
    authed.put("/api/settings", json={k: "" for k in keys})
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


def _session():
    from sqlmodel import Session
    from app.db import engine
    return Session(engine)


def test_nothing_is_sent_until_a_destination_is_chosen(authed, clean_offsite):
    with _session() as s:
        assert offsite.settings(s) is None and offsite.run(s) is None
    assert authed.post("/api/backup/offsite/test").status_code == 502
    assert authed.post("/api/backup/offsite/now").status_code == 400


def test_a_folder_gets_the_newest_backup_once_and_only_keeps_a_few(authed, clean_offsite):
    folder = clean_offsite / "dest"
    folder.mkdir()
    authed.put("/api/settings", json={"offsite_kind": "folder", "offsite_path": str(folder), "offsite_keep": "2"})
    assert authed.post("/api/backup/offsite/test").json()["where"].endswith("modelhub-offsite-test.txt")
    (folder / "unrelated.zip").write_bytes(b"mine")
    r = authed.post("/api/backup/offsite/now")
    assert r.status_code == 200 and (folder / r.json()["where"].replace("\\", "/").rsplit("/", 1)[-1]).is_file()
    with _session() as s:
        assert offsite.run(s) is None                                              # that one is already there
    for _ in range(3):
        authed.post("/api/backup/save")
        with _session() as s:
            offsite.run(s)
    backups = sorted(p.name for p in folder.glob("*.zip") if offsite.BACKUP_NAME.match(p.name))
    assert len(backups) == 2 and (folder / "unrelated.zip").is_file()              # only Model Hub's own files are ever pruned
    assert not list(folder.glob("*.part"))


@pytest.mark.parametrize("path", ["relative/dir", "/does/not/exist/anywhere", "/tmp/../etc", ""])
def test_a_bad_folder_is_refused_with_a_reason(authed, clean_offsite, path):
    authed.put("/api/settings", json={"offsite_kind": "folder", "offsite_path": path})
    r = authed.post("/api/backup/offsite/test")
    assert r.status_code == 502 and ("folder" in r.json()["detail"] or "Choose" in r.json()["detail"])


def test_a_webdav_server_receives_the_file_with_the_password_and_errors_are_explained(authed, clean_offsite, monkeypatch):
    calls = []

    def put(url, content=None, auth=None, **kw):
        calls.append((url, auth, content.read(4)))
        return httpx.Response(calls_status[0], request=httpx.Request("PUT", url))
    calls_status = [201]
    monkeypatch.setattr(offsite.httpx, "put", put)
    authed.put("/api/settings", json={"offsite_kind": "webdav", "offsite_url": "https://cloud.example/remote.php/dav/files/me/backups/", "offsite_user": "me", "offsite_password": "s3cret"})
    r = authed.post("/api/backup/offsite/test")
    assert r.status_code == 200 and calls[0][0] == "https://cloud.example/remote.php/dav/files/me/backups/modelhub-offsite-test.txt"
    assert calls[0][1] == ("me", "s3cret") and "s3cret" not in r.text
    assert authed.get("/api/settings").json()["offsite_password"] == "********"
    calls_status[0] = 401
    assert "refused the user name or password" in authed.post("/api/backup/offsite/test").json()["detail"]
    calls_status[0] = 507
    assert "507" in authed.post("/api/backup/offsite/test").json()["detail"]


@pytest.mark.parametrize("url", ["ftp://x/y", "https://user:pw@cloud.example/dav", "cloud.example/dav", "https://cloud.example/dav?x=1"])
def test_unsafe_webdav_addresses_are_refused(authed, clean_offsite, url):
    authed.put("/api/settings", json={"offsite_kind": "webdav", "offsite_url": url})
    assert authed.post("/api/backup/offsite/test").status_code == 502


def test_the_password_stays_out_of_backups_and_a_failure_is_remembered(authed, clean_offsite):
    import io
    import zipfile
    authed.put("/api/settings", json={"offsite_kind": "folder", "offsite_path": "/nowhere/at/all", "offsite_password": "very-secret-pw"})
    authed.post("/api/backup/save")
    with _session() as s:
        offsite.scheduled(s)                                                       # logged and remembered, never raised
    assert authed.get("/api/settings").json()["offsite_last_error"]
    data = authed.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert b"very-secret-pw" not in z.read("modelhub.db")


def test_the_scheduler_has_the_offsite_job_and_only_the_administrator_may_use_it(authed, clean_offsite):
    from app import scheduler
    from app.main import app
    assert "offsite" in scheduler.INTERVALS and "offsite" in scheduler._jobs()
    authed.post("/api/users", json={"username": "offmember", "password": PW, "role": "member"})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "offmember", "password": PW})
        assert member.post("/api/backup/offsite/now").status_code == 403
        assert member.put("/api/settings", json={"offsite_kind": "folder"}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
