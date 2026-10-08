"""Printers on the network: status, and sending G-code (uploaded, or sliced from a model by a stand-in slicer)."""
import io
import sqlite3
import stat
import zipfile

import httpx
import pytest

from app import printers as printing
from app.config import DB_PATH

KEY = "printer-secret-key-1234"


class FakePrinter:
    """A stand-in for a Moonraker and an OctoPrint host, recording what it was sent."""

    def __init__(self):
        self.requests = []
        self.uploads = []                 # (path, raw request body)
        self.fail = None                  # an exception to raise instead of answering
        self.moonraker_state = "printing"
        self.moonraker_status_code = 200
        self.octoprint_status = 200
        self.upload_status = 200
        self.print_started = True
        self.key_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path, request.headers.get("x-api-key")))
        if self.fail:
            raise self.fail
        if not self.key_ok:
            return httpx.Response(403)
        path, host = request.url.path, request.url.host
        if host == "redirecting.local":
            return httpx.Response(302, headers={"location": "http://elsewhere.example/"})
        if host == "klipper.local":
            if path == "/printer/objects/query":
                if self.moonraker_status_code != 200:
                    return httpx.Response(self.moonraker_status_code)
                return httpx.Response(200, json={"result": {"status": {
                    "print_stats": {"state": self.moonraker_state, "filename": "benchy.gcode", "print_duration": 754.0},
                    "display_status": {"progress": 0.4237}, "extruder": {"temperature": 214.96}, "heater_bed": {"temperature": 59.9}}}})
            if path == "/server/files/upload":
                return self._upload(request, {"item": {"path": "x"}, "print_started": self.print_started and b"true" in request.content})
        if host == "octopi.local":
            if path == "/api/printer":
                if self.octoprint_status != 200:
                    return httpx.Response(self.octoprint_status)
                return httpx.Response(200, json={"state": {"text": "Printing", "flags": {"printing": True, "ready": False}},
                                                 "temperature": {"tool0": {"actual": 200.2}, "bed": {"actual": 60}}})
            if path == "/api/job":
                return httpx.Response(200, json={"job": {"file": {"name": "part.gcode"}}, "progress": {"completion": 12.345}})
            if path == "/api/files/local":
                return self._upload(request, {"done": True}, status=201)
        return httpx.Response(404)

    def _upload(self, request, body, status=200):
        self.uploads.append((request.url.path, request.content))
        return httpx.Response(self.upload_status if self.upload_status != 200 else status, json=body)


@pytest.fixture()
def fake(monkeypatch, authed):
    f = FakePrinter()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    yield f
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")


def _add(c, **fields):
    payload = {"name": "Voron", "kind": "moonraker", "url": "http://klipper.local:7125", **fields}
    r = c.post("/api/printers", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _gcode(name="part.gcode", data=b"G28\nG1 X10 Y10\n"):
    return {"file": (name, data, "application/octet-stream")}


# ---------- adding and editing ----------

def test_a_printer_is_added_with_a_tidy_address_and_the_key_never_comes_back(authed, fake):
    p = _add(authed, url="  klipper.local:7125/  ", api_key=KEY)
    assert p["url"] == "http://klipper.local:7125" and p["has_key"] is True and "api_key" not in p
    listing = authed.get("/api/printers").json()
    assert listing["printers"][0]["has_key"] is True and KEY not in str(listing)
    assert authed.patch(f"/api/printers/{p['id']}", json={"name": "Renamed"}).json()["has_key"] is True       # the key stays
    assert authed.patch(f"/api/printers/{p['id']}", json={"api_key": ""}).json()["has_key"] is False        # "" clears it
    assert authed.delete(f"/api/printers/{p['id']}").status_code == 200
    assert authed.delete(f"/api/printers/{p['id']}").status_code == 404


@pytest.mark.parametrize("payload", [
    {"name": ""}, {"name": 5}, {"kind": "marlin"}, {"url": ""}, {"url": "ftp://x.local"}, {"url": "http://user:pw@x.local"},
    {"url": "http://x.local/?a=1"}, {"kind": "octoprint", "url": "http://octopi.local"},       # OctoPrint needs a key
])
def test_bad_printers_are_refused(authed, fake, payload):
    r = authed.post("/api/printers", json={"name": "P", "kind": "moonraker", "url": "http://klipper.local", **payload})
    assert r.status_code == 400
    assert authed.get("/api/printers").json()["printers"] == []


def test_the_slicer_is_reported_as_off_unless_both_paths_are_set(authed, fake, monkeypatch):
    monkeypatch.delenv("SLICER_CLI_PATH", raising=False)
    monkeypatch.delenv("SLICER_CONFIG_PATH", raising=False)
    info = authed.get("/api/printers").json()
    assert info["slicer_ready"] is False and "SLICER_CONFIG_PATH" in info["slicer_note"]


# ---------- status ----------

def test_moonraker_status(authed, fake):
    p = _add(authed, api_key=KEY)
    s = authed.get(f"/api/printers/{p['id']}/status").json()
    assert s == {"online": True, "state": "printing", "progress": 42.4, "file": "benchy.gcode", "nozzle": 215.0, "bed": 59.9, "message": None, "duration": 754.0}
    assert ("GET", "/printer/objects/query", KEY) in fake.requests


def test_octoprint_status(authed, fake):
    p = _add(authed, name="Octo", kind="octoprint", url="http://octopi.local", api_key=KEY)
    s = authed.get(f"/api/printers/{p['id']}/status").json()
    assert s["online"] is True and s["state"] == "printing" and s["file"] == "part.gcode" and s["progress"] == 12.3
    assert s["nozzle"] == 200.2 and s["bed"] == 60.0


def test_octoprint_not_connected_and_klipper_not_ready(authed, fake):
    octo = _add(authed, name="Octo", kind="octoprint", url="http://octopi.local", api_key=KEY)
    fake.octoprint_status = 409
    s = authed.get(f"/api/printers/{octo['id']}/status").json()
    assert s["online"] is True and s["state"] == "not connected" and "not connected" in s["message"]
    klip = _add(authed)
    fake.moonraker_status_code = 503
    s = authed.get(f"/api/printers/{klip['id']}/status").json()
    assert s["online"] is True and "not ready" in s["message"]


@pytest.mark.parametrize("failure,fragment", [
    (httpx.ConnectError("boom"), "Could not reach"), (httpx.ReadTimeout("slow"), "did not answer in time"),
])
def test_an_unreachable_printer_is_reported_not_raised(authed, fake, failure, fragment):
    p = _add(authed, api_key=KEY)
    fake.fail = failure
    s = authed.get(f"/api/printers/{p['id']}/status").json()
    assert s["online"] is False and s["state"] == "offline" and fragment in s["message"] and KEY not in str(s)


def test_a_refused_key_and_a_redirecting_address_have_clear_messages(authed, fake):
    p = _add(authed, api_key="wrong")
    fake.key_ok = False
    assert "refused the API key" in authed.get(f"/api/printers/{p['id']}/status").json()["message"]
    fake.key_ok = True
    r = _add(authed, name="Redirect", url="http://redirecting.local")
    assert "redirects" in authed.get(f"/api/printers/{r['id']}/status").json()["message"]
    assert authed.get("/api/printers/987654/status").status_code == 404


# ---------- sending ----------

def test_a_gcode_file_is_uploaded_to_moonraker_without_starting_it(authed, fake):
    p = _add(authed, api_key=KEY)
    r = authed.post(f"/api/printers/{p['id']}/send", files=_gcode("My Part (v2).gcode"), data={"start": "false"})
    assert r.status_code == 200 and r.json() == {"filename": "My_Part_v2.gcode", "started": False}
    path, body = fake.uploads[0]
    assert path == "/server/files/upload" and b'name="print"\r\n\r\nfalse' in body and b"G28" in body and b"My_Part_v2.gcode" in body
    assert ("POST", "/server/files/upload", KEY) in fake.requests


def test_starting_the_print_is_only_done_when_asked(authed, fake):
    p = _add(authed, api_key=KEY)
    r = authed.post(f"/api/printers/{p['id']}/send", files=_gcode(), data={"start": "true"})
    assert r.json()["started"] is True and b'name="print"\r\n\r\ntrue' in fake.uploads[0][1]
    fake.print_started = False
    r = authed.post(f"/api/printers/{p['id']}/send", files=_gcode(), data={"start": "true"})
    assert r.status_code == 502 and "did not start it" in r.json()["detail"]


def test_a_gcode_file_is_uploaded_to_octoprint(authed, fake):
    p = _add(authed, name="Octo", kind="octoprint", url="http://octopi.local", api_key=KEY)
    r = authed.post(f"/api/printers/{p['id']}/send", files=_gcode("a.gcode"), data={"start": "true"})
    assert r.status_code == 200 and r.json() == {"filename": "a.gcode", "started": True}
    path, body = fake.uploads[0]
    assert path == "/api/files/local" and b'name="select"' in body and b'name="print"\r\n\r\ntrue' in body


@pytest.mark.parametrize("status,fragment", [(409, "busy"), (413, "too large"), (500, "did not accept")])
def test_upload_problems_become_messages(authed, fake, status, fragment):
    p = _add(authed, api_key=KEY)
    fake.upload_status = status
    r = authed.post(f"/api/printers/{p['id']}/send", files=_gcode(), data={})
    assert r.status_code == 502 and fragment in r.json()["detail"] and KEY not in r.text


def test_only_gcode_and_a_real_file_can_be_sent(authed, fake):
    p = _add(authed)
    assert authed.post(f"/api/printers/{p['id']}/send", files=_gcode("model.stl"), data={}).status_code == 400
    assert authed.post(f"/api/printers/{p['id']}/send", files=_gcode("empty.gcode", b""), data={}).status_code == 502
    assert authed.post(f"/api/printers/{p['id']}/send", data={}).status_code == 400
    assert authed.post("/api/printers/987654/send", files=_gcode(), data={}).status_code == 404
    assert fake.uploads == []


# ---------- slicing a model first ----------

def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": ("slice me.stl", _stl(66), "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


def _stub_slicer(tmp_path, monkeypatch, body):
    script = tmp_path / "fakeslicer.sh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    config = tmp_path / "printer.ini"
    config.write_text("; profile\n")
    monkeypatch.setenv("SLICER_CLI_PATH", str(script))
    monkeypatch.setenv("SLICER_CONFIG_PATH", str(config))
    return script


WRITE_GCODE = 'out=""; while [ $# -gt 0 ]; do if [ "$1" = "-o" ]; then out="$2"; fi; shift; done; echo "G28" > "$out"\n'


def test_slicing_is_refused_without_a_slicer_and_a_profile(authed, fake, model, monkeypatch):
    monkeypatch.delenv("SLICER_CLI_PATH", raising=False)
    p = _add(authed)
    r = authed.post(f"/api/printers/{p['id']}/send", data={"model_id": str(model["id"])})
    assert r.status_code == 502 and "Slicing here is off" in r.json()["detail"] and fake.uploads == []


def test_a_model_is_sliced_with_the_profile_then_sent(authed, fake, model, tmp_path, monkeypatch):
    log = tmp_path / "args.txt"
    _stub_slicer(tmp_path, monkeypatch, f'echo "$@" > {log}\n' + WRITE_GCODE)
    p = _add(authed, api_key=KEY)
    r = authed.post(f"/api/printers/{p['id']}/send", data={"model_id": str(model["id"]), "start": "false", "infill": "0.2"})
    assert r.status_code == 200, r.text
    assert r.json()["filename"].startswith("slice_me") and r.json()["filename"].endswith(".gcode") and r.json()["started"] is False
    args = log.read_text()
    assert "--load" in args and "printer.ini" in args and "--fill-density 20%" in args and "--export-gcode" in args
    assert b"G28" in fake.uploads[0][1]


def test_slicer_failures_are_reported_and_nothing_is_uploaded(authed, fake, model, tmp_path, monkeypatch):
    _stub_slicer(tmp_path, monkeypatch, "exit 3\n")
    p = _add(authed)
    r = authed.post(f"/api/printers/{p['id']}/send", data={"model_id": str(model["id"])})
    assert r.status_code == 502 and "could not slice" in r.json()["detail"]
    _stub_slicer(tmp_path, monkeypatch, "exit 0\n")                          # "succeeds" but writes nothing
    r = authed.post(f"/api/printers/{p['id']}/send", data={"model_id": str(model["id"])})
    assert r.status_code == 502 and "did not produce any G-code" in r.json()["detail"]
    assert fake.uploads == []


def test_models_that_cannot_be_sliced_or_found(authed, fake, model, tmp_path, monkeypatch):
    _stub_slicer(tmp_path, monkeypatch, WRITE_GCODE)
    p = _add(authed)
    assert authed.post(f"/api/printers/{p['id']}/send", data={"model_id": "987654"}).status_code == 404
    assert authed.post(f"/api/printers/{p['id']}/send", data={"model_id": str(model["id"]), "infill": "3"}).status_code == 400


# ---------- keys, backups, access ----------

def test_keys_are_left_out_of_backups_and_survive_a_restore(authed, fake):
    p = _add(authed, api_key=KEY)
    data = authed.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extract("modelhub.db", path=str(DB_PATH.parent / "peek_printers"))
    assert KEY.encode() not in data
    conn = sqlite3.connect(DB_PATH.parent / "peek_printers" / "modelhub.db")
    try:
        assert conn.execute("SELECT name, api_key FROM printer").fetchall() == [("Voron", None)]
    finally:
        conn.close()
    r = authed.post("/api/backup/restore", files={"file": ("b.zip", data)}, data={"confirm": "replace"})
    assert r.status_code == 200
    assert authed.get("/api/printers").json()["printers"][0]["has_key"] is True        # same printer, key kept
    s = authed.get(f"/api/printers/{p['id']}/status").json()
    assert s["online"] is True and ("GET", "/printer/objects/query", KEY) in fake.requests
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


def test_printers_are_for_the_administrator_only(authed, fake):
    from starlette.testclient import TestClient
    from app.main import app
    authed.post("/api/users", json={"username": "printermember", "password": "a long enough password"})
    try:
        member = TestClient(app)
        assert member.post("/api/auth/login", json={"username": "printermember", "password": "a long enough password"}).status_code == 200
        assert member.get("/api/printers").status_code == 403
        assert member.post("/api/printers", json={}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert TestClient(app).get("/api/printers").status_code == 401
