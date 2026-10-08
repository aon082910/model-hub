"""Bambu Lab printers in LAN mode: status, the AMS, and spools suggested from what the AMS reports.
The printer is never contacted: _bambu_report is replaced by a function that returns what a printer's MQTT report looks like."""
import uuid

import pytest
from starlette.testclient import TestClient

from app import printers as printing, printwatch

PW = "a long enough password"
SERIAL = "01P00A123456789"


def report(**changes):
    base = {"gcode_state": "RUNNING", "mc_percent": 42, "subtask_name": "benchy.gcode.3mf", "nozzle_temper": 215.2, "bed_temper": 60.0,
            "ams": {"ams": [{"id": "0", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 80, "tray_sub_brands": "PLA Basic"},
                {"id": "1", "tray_type": "PETG", "tray_color": "0000FFFF", "remain": -1},
                {"id": "2"},
                {"id": "3", "tray_type": "ABS", "tray_color": "00FF00FF", "remain": 100}]}]}}
    base.update(changes)
    return base


@pytest.fixture()
def bambu(monkeypatch, authed):
    state = {"report": report(), "error": None, "calls": []}

    def fake(host, serial, code, timeout=printing.BAMBU_TIMEOUT):
        state["calls"].append((host, serial, code))
        if state["error"]:
            raise printing.PrinterError(state["error"])
        return state["report"]

    monkeypatch.setattr(printing, "_bambu_report", fake)
    printwatch.reset()
    yield state
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")


def _add(c, **extra):
    body = {"name": "P1S", "kind": "bambu", "url": "192.168.1.60", "serial": SERIAL.lower(), "api_key": "12345678", **extra}
    r = c.post("/api/printers", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_bambu_printer_needs_its_address_serial_number_and_access_code(authed, bambu):
    p = _add(authed, url="http://192.168.1.60:8883/")
    assert p["url"] == "192.168.1.60" and p["serial"] == SERIAL and p["slot_count"] == 4 and p["has_key"] is True and "api_key" not in p
    for bad in ({"serial": ""}, {"serial": "short"}, {"serial": "has space 12345"}, {"api_key": ""}, {"url": ""}, {"url": "bad host!"}, {"url": "a/../b c"}):
        r = authed.post("/api/printers", json={"name": "x", "kind": "bambu", "url": "192.168.1.61", "serial": SERIAL, "api_key": "12345678", **bad})
        assert r.status_code == 400, bad


def test_the_status_shows_state_progress_temperatures_and_the_ams(authed, bambu):
    p = _add(authed)
    s = authed.get(f"/api/printers/{p['id']}/status").json()
    assert (s["online"], s["state"], s["progress"], s["file"], s["nozzle"], s["bed"]) == (True, "printing", 42.0, "benchy.gcode.3mf", 215.2, 60.0)
    assert bambu["calls"][-1] == ("192.168.1.60", SERIAL, "12345678")
    ams = {t["slot"]: t for t in s["ams"]}
    assert ams[1] == {"slot": 1, "material": "PLA", "color": "#ff0000", "remain": 80, "name": "PLA Basic", "empty": False}
    assert ams[2]["remain"] is None and ams[3]["empty"] is True and ams[4]["material"] == "ABS"
    assert "12345678" not in str(s)


@pytest.mark.parametrize("raw,shown", [("IDLE", "standby"), ("FINISH", "complete"), ("FAILED", "error"), ("PAUSE", "paused"), ("PREPARE", "printing"), ("ODD", "odd")])
def test_states_are_translated(authed, bambu, raw, shown):
    p = _add(authed)
    bambu["report"] = report(gcode_state=raw)
    assert authed.get(f"/api/printers/{p['id']}/status").json()["state"] == shown


def test_an_unreachable_printer_is_offline_with_a_reason_not_an_error(authed, bambu):
    p = _add(authed)
    bambu["error"] = "The printer refused the access code"
    s = authed.get(f"/api/printers/{p['id']}/status")
    assert s.status_code == 200 and s.json()["online"] is False and "access code" in s.json()["message"]


def test_files_are_not_sent_to_a_bambu_printer(authed, bambu):
    p = _add(authed)
    r = authed.post(f"/api/printers/{p['id']}/send", files={"file": ("a.gcode", b"G28\n", "application/octet-stream")}, data={"start": "false"})
    assert r.status_code == 502 and "Bambu Studio" in r.json()["detail"]


def test_a_finished_print_is_recorded_like_on_the_other_printers(authed, bambu):
    p = _add(authed)
    n = 30 + uuid.uuid4().int % 250
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()
    m = authed.post("/api/library/import", files={"file": (f"bb_{uuid.uuid4().hex[:6]}.stl", stl, "application/octet-stream")}).json()
    try:
        from sqlmodel import Session
        from app.db import engine
        from app.models import PrinterJob
        with Session(engine) as s:
            s.add(PrinterJob(printer_id=p["id"], filename="benchy.gcode.3mf", model_id=m["id"], started=True))
            s.commit()
            printwatch.poll(s)                                                       # RUNNING
            bambu["report"] = report(gcode_state="FINISH", mc_percent=100)
            assert printwatch.poll(s) == [("P1S", "done")]
        log = authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"][0]
        assert log["source"] == "printer" and log["printer_id"] == p["id"] and log["outcome"] is None
        assert printwatch.latest[p["id"]]["ams"]                                    # and what the AMS said is kept for the slots panel
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_failed_print_is_logged_as_a_failure(authed, bambu):
    from sqlmodel import Session
    from app.db import engine
    p = _add(authed)
    with Session(engine) as s:
        printwatch.poll(s)
        bambu["report"] = report(gcode_state="FAILED")
        assert printwatch.poll(s) == [("P1S", "stopped")]


# ---------- the AMS and the slots panel ----------

def _spool(c, material, color, hex_, grams=500):
    return c.post("/api/filament", json={"material": material, "color": color, "color_hex": hex_, "spool_weight_g": 1000, "remaining_g": grams}).json()


def _slots(c):
    return {s["slot"]: s for s in c.get("/api/slots").json()["printers"][0]["slots"]}


def test_the_slots_panel_shows_what_the_ams_reports_and_suggests_a_matching_spool(authed, bambu):
    p = _add(authed)
    red, blue, other = _spool(authed, "PLA", "red", "#ee1111"), _spool(authed, "PETG", "blue", "#0000ee"), _spool(authed, "PLA", "pink", "#ffb6c1")
    try:
        authed.get(f"/api/printers/{p['id']}/status")
        overview = authed.get("/api/slots").json()["printers"][0]
        assert overview["reads_slots"] is True and overview["kind"] == "bambu"
        slots = _slots(authed)
        assert slots[1]["reported"]["material"] == "PLA" and slots[1]["suggested_filament_id"] == red["id"]
        assert slots[2]["suggested_filament_id"] == blue["id"]
        assert slots[3]["reported"]["empty"] is True and slots[3]["suggested_filament_id"] is None
        assert slots[4]["suggested_filament_id"] is None                                   # no ABS spool
        authed.put(f"/api/slots/{p['id']}/1", json={"filament_id": other["id"]})          # a choice of yours is never replaced
        assert _slots(authed)[1]["suggested_filament_id"] is None
    finally:
        for s in (red, blue, other):
            authed.delete(f"/api/filament/{s['id']}")


def test_suggestions_are_applied_to_empty_slots_only(authed, bambu):
    p = _add(authed)
    red, blue = _spool(authed, "PLA", "red", "#ee1111"), _spool(authed, "PETG", "blue", "#0000ee")
    try:
        authed.get(f"/api/printers/{p['id']}/status")
        authed.put(f"/api/slots/{p['id']}/2", json={"filament_id": blue["id"]})
        done = authed.post(f"/api/slots/{p['id']}/apply-suggestions").json()
        assert done == {"loaded": [1]}
        slots = _slots(authed)
        assert slots[1]["spool"]["id"] == red["id"] and slots[2]["spool"]["id"] == blue["id"]
        assert authed.post(f"/api/slots/{p['id']}/apply-suggestions").json() == {"loaded": []}
        assert authed.post("/api/slots/987654/apply-suggestions").status_code == 404
    finally:
        for s in (red, blue):
            authed.delete(f"/api/filament/{s['id']}")


def test_remaining_weight_can_be_taken_from_the_ams_where_it_knows(authed, bambu):
    p = _add(authed)
    red, blue = _spool(authed, "PLA", "red", "#ee1111", 500), _spool(authed, "PETG", "blue", "#0000ee", 500)
    try:
        authed.get(f"/api/printers/{p['id']}/status")
        authed.put(f"/api/slots/{p['id']}/1", json={"filament_id": red["id"]})
        authed.put(f"/api/slots/{p['id']}/2", json={"filament_id": blue["id"]})                # slot 2 reports no percentage
        done = authed.post(f"/api/slots/{p['id']}/sync-remaining").json()["changed"]
        assert done == [{"slot": 1, "spool_id": red["id"], "from": 500, "to": 800.0}]
        spools = {f["id"]: f for f in authed.get("/api/filament").json()}
        assert spools[red["id"]]["remaining_g"] == 800 and spools[blue["id"]]["remaining_g"] == 500
        assert authed.post(f"/api/slots/{p['id']}/sync-remaining").json()["changed"] == []
    finally:
        for s in (red, blue):
            authed.delete(f"/api/filament/{s['id']}")


def test_only_bambu_printers_have_something_reported(authed, bambu):
    plain = authed.post("/api/printers", json={"name": "Plain", "kind": "moonraker", "url": "http://klipper.local:7125", "slot_count": 2}).json()
    slots = authed.get("/api/slots").json()["printers"][0]
    assert slots["reads_slots"] is False and all(s["reported"] is None for s in slots["slots"]) and plain["serial"] is None


def test_the_real_connection_code_reports_problems_without_a_printer(monkeypatch):
    """With nothing listening, the real function gives a readable error instead of hanging or raising something else."""
    monkeypatch.setattr(printing, "BAMBU_PORT", 1)
    with pytest.raises(printing.PrinterError) as e:
        printing._bambu_report("127.0.0.1", SERIAL, "12345678", timeout=2)
    assert "Could not reach the printer" in str(e.value) or "did not answer" in str(e.value)


def test_only_the_administrator_adds_bambu_printers_and_viewers_cannot_load_slots(authed, bambu):
    from app.main import app
    p = _add(authed)
    authed.post("/api/users", json={"username": "bbviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "bbviewer", "password": PW})
        assert viewer.post("/api/printers", json={"name": "x", "kind": "bambu", "url": "1.2.3.4", "serial": SERIAL, "api_key": "12345678"}).status_code == 403
        assert viewer.post(f"/api/slots/{p['id']}/apply-suggestions").status_code == 403
        assert viewer.get("/api/slots").status_code == 200
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
