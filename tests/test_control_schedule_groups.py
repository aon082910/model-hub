"""Heat, speed, fan, light and skipping an object; prints that start by themselves; access groups; reprinting on another printer; comparing two prints; themes and shortcuts."""
import json
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import Session
from starlette.testclient import TestClient

from app import auth, printers as printing, printwatch, start_at
from app.db import engine
from app.models import AccessGroup, AppUser, PrintLog, Printer, QueueItem
from test_printers import FakePrinter
from test_print_farm import farm, _keep, _queue  # noqa: F401  (the fixture and helpers)

PW = "a long enough password"


def _app():
    from app.main import app
    return app


@pytest.fixture(autouse=True)
def _clean_settings(authed):
    yield
    authed.put("/api/settings", json={"scheduled_starts": "", "plate_clear_gate": "", "stagger_minutes": ""})
    start_at.reset()


def _login(name, password=PW):
    auth._login_attempts.clear()
    c = TestClient(_app())
    assert c.post("/api/auth/login", json={"username": name, "password": password}).status_code == 200
    return c


class Rich(FakePrinter):
    """Also records what it is told to run, and knows Klipper's labelled objects."""

    def __init__(self):
        super().__init__()
        self.sent = []                                   # (path, parsed body)
        self.objects = {"objects": [{"name": "cube_1"}, {"name": "cube_2"}], "excluded_objects": ["cube_2"], "current_object": "cube_1"}
        self.command_status = 200

    def __call__(self, request):
        path = request.url.path
        if request.method == "POST" and path in ("/printer/gcode/script", "/api/printer/command"):
            self.sent.append((path, json.loads(request.content)))
            return httpx.Response(self.command_status, json={"result": "ok"})
        if request.url.host == "klipper.local" and path == "/printer/objects/query" and "exclude_object" in str(request.url.query):
            return httpx.Response(200, json={"result": {"status": {"exclude_object": self.objects}}})
        return super().__call__(request)


@pytest.fixture()
def rich(monkeypatch, authed):
    f = Rich()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    printwatch.reset()
    start_at.reset()
    yield f
    authed.put("/api/settings", json={"scheduled_starts": "", "plate_clear_gate": "", "stagger_minutes": ""})
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")
    for u in authed.get("/api/users").json()["users"]:
        authed.delete(f"/api/users/{u['id']}")
    for g in authed.get("/api/access-groups").json()["groups"]:
        authed.delete(f"/api/access-groups/{g['id']}")
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")
    printwatch.reset()


def _printer(c, name="Rich", kind="moonraker", url="http://klipper.local:7125", **extra):
    r = c.post("/api/printers", json={"name": name, "kind": kind, "url": url, **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- heat, speed, fan
def test_klipper_gets_plain_gcode_for_each_change(authed, rich):
    p = _printer(authed)
    url = f"/api/printers/{p['id']}/adjust"
    for payload, line in (({"nozzle": 210}, "M104 S210"), ({"bed": 60.5}, "M140 S60.5"), ({"speed": 120}, "M220 S120"), ({"fan": 50}, "M106 S128"), ({"fan": 0}, "M106 S0"), ({"nozzle": 0}, "M104 S0")):
        r = authed.post(url, json=payload)
        assert r.status_code == 200 and r.json()["status"] == "sent", (payload, r.text)
        assert rich.sent[-1] == ("/printer/gcode/script", {"script": line}), payload
    assert authed.post(url, json={"nozzle": 210, "bed": 60}).status_code == 400 and authed.post(url, json={}).status_code == 400                # one thing at a time
    for bad in ({"nozzle": 301}, {"nozzle": -1}, {"nozzle": "hot"}, {"nozzle": True}, {"nozzle": None}, {"bed": 121}, {"speed": 9}, {"speed": 301}, {"fan": 101}, {"fan": -5}, {"light": True}):
        before = len(rich.sent)
        r = authed.post(url, json=bad)
        assert r.status_code == 400 and len(rich.sent) == before, (bad, r.text)                                                        # nothing reaches the printer
    assert "Only Bambu Lab" in authed.post(url, json={"light": True}).json()["detail"]
    assert authed.post("/api/printers/999999/adjust", json={"fan": 10}).status_code == 404
    rich.command_status = 400
    assert authed.post(url, json={"fan": 10}).status_code == 502
    rich.command_status = 200
    rich.fail = httpx.ConnectError("down")
    r = authed.post(url, json={"fan": 10})
    assert r.status_code == 502
    rich.fail = None
    assert any("nozzle set to 210" in a["summary"] for a in authed.get("/api/activity").json()["items"])


def test_octoprint_gets_the_commands_in_its_own_form(authed, rich):
    p = _printer(authed, "Octo", "octoprint", "http://octopi.local", api_key="octokey123")
    assert authed.post(f"/api/printers/{p['id']}/adjust", json={"bed": 55}).status_code == 200
    assert rich.sent[-1] == ("/api/printer/command", {"commands": ["M140 S55"]})
    assert authed.post(f"/api/printers/{p['id']}/adjust", json={"speed": 80}).status_code == 200 and rich.sent[-1][1] == {"commands": ["M220 S80"]}


def test_bambu_gets_mqtt_messages_and_a_speed_level(authed, monkeypatch):
    sent = []
    monkeypatch.setattr(printing, "_bambu_publish", lambda host, serial, code, payload, timeout=8.0: sent.append((host, serial, code, payload)))
    monkeypatch.setattr(printing, "status", lambda *a, **k: {"online": True, "state": "idle", "progress": None, "file": None, "nozzle": 20.0, "bed": 20.0, "message": None})
    p = authed.post("/api/printers", json={"name": "Bam", "kind": "bambu", "url": "192.168.1.77", "serial": "01P00A123456789", "api_key": "12345678"}).json()
    try:
        url = f"/api/printers/{p['id']}/adjust"
        assert authed.post(url, json={"nozzle": 200}).status_code == 200
        assert sent[-1][3] == {"print": {"sequence_id": "2", "command": "gcode_line", "param": "M104 S200\n"}} and sent[-1][:3] == ("192.168.1.77", "01P00A123456789", "12345678")
        assert authed.post(url, json={"speed": 3}).status_code == 200 and sent[-1][3] == {"print": {"sequence_id": "2", "command": "print_speed", "param": "3"}}
        assert authed.post(url, json={"speed": 5}).status_code == 400 and authed.post(url, json={"speed": 100}).status_code == 400
        assert authed.post(url, json={"light": False}).status_code == 200
        led = sent[-1][3]["system"]
        assert led["command"] == "ledctrl" and led["led_node"] == "chamber_light" and led["led_mode"] == "off"
        assert authed.post(url, json={"light": "on"}).status_code == 400
        assert authed.post(f"/api/printers/{p['id']}/skip-object", json={"name": "cube_1"}).status_code == 409                     # (it is idle)
        assert authed.get(f"/api/printers/{p['id']}/objects").json() == {"supported": False, "objects": []}
    finally:
        authed.delete(f"/api/printers/{p['id']}")


def test_an_unreachable_printer_is_not_told_anything(authed, rich):
    p = _printer(authed)
    rich.moonraker_status_code = 500
    r = authed.post(f"/api/printers/{p['id']}/adjust", json={"fan": 10})
    assert r.status_code == 502 and rich.sent == []


# ---------------------------------------------------------------- skipping an object
def test_objects_are_listed_and_one_can_be_skipped_while_printing(authed, rich):
    p = _printer(authed)
    got = authed.get(f"/api/printers/{p['id']}/objects").json()
    assert got == {"supported": True, "objects": [{"name": "cube_1", "excluded": False, "current": True}, {"name": "cube_2", "excluded": True, "current": False}]}
    r = authed.post(f"/api/printers/{p['id']}/skip-object", json={"name": "cube_1"})
    assert r.status_code == 200 and rich.sent[-1] == ("/printer/gcode/script", {"script": "EXCLUDE_OBJECT NAME=cube_1"})
    for bad in ("cube 1", "x; M112", "a\nM112", "", None, 5, "n" * 200, "a=b c"):
        before = len(rich.sent)
        assert authed.post(f"/api/printers/{p['id']}/skip-object", json={"name": bad}).status_code == 400 and len(rich.sent) == before, bad       # no G-code can be smuggled in
    rich.moonraker_state = "standby"
    assert authed.post(f"/api/printers/{p['id']}/skip-object", json={"name": "cube_1"}).status_code == 409
    octo = _printer(authed, "Octo", "octoprint", "http://octopi.local", api_key="octokey123")
    assert authed.get(f"/api/printers/{octo['id']}/objects").json()["supported"] is False
    assert "Klipper" in authed.post(f"/api/printers/{octo['id']}/skip-object", json={"name": "cube_1"}).json()["detail"]
    rich.objects = {}
    assert authed.get(f"/api/printers/{p['id']}/objects").json() == {"supported": True, "objects": []}


def test_only_the_administrator_may_change_a_printer(authed, rich):
    p = _printer(authed)
    authed.post("/api/users", json={"username": "member29", "password": PW, "role": "member"})
    authed.post("/api/users", json={"username": "printer29", "password": PW, "role": "printer"})
    for name in ("member29", "printer29"):
        c = _login(name)
        for path, body in (("adjust", {"nozzle": 200}), ("skip-object", {"name": "cube_1"}), ("diagnose", {})):
            assert c.post(f"/api/printers/{p['id']}/{path}", json=body).status_code == 403, (name, path)
    assert rich.sent == []


# ---------------------------------------------------------------- starting by itself
def _ready(authed, farm_fixture, **fields):
    fake, a, b, m, _ = farm_fixture
    _keep(authed, m)
    fake.moonraker_state = "standby"
    return _queue(authed, farm_fixture, printer_id=a["id"], **fields)


def _when(minutes):
    return (datetime.utcnow() + timedelta(minutes=minutes)).isoformat(timespec="seconds") + "Z"


def test_a_start_time_is_checked(authed, farm):
    fake, a, b, m, _ = farm
    item = _ready(authed, farm)
    url = f"/api/queue/{item['id']}"
    got = authed.patch(url, json={"start_at": _when(30)}).json()
    assert got["start_at"] and datetime.fromisoformat(got["start_at"]) > datetime.utcnow()
    assert authed.patch(url, json={"start_at": ""}).json()["start_at"] is None
    for bad, code in (("tomorrow", 400), (12345, 400), (_when(-60), 400), (_when(60 * 24 * 400), 400), ("2026-13-45T00:00:00Z", 400)):
        assert authed.patch(url, json={"start_at": bad}).status_code == code, bad
    assert authed.patch(url, json={"start_at": "2099-01-01T00:00:00+02:00"}).status_code == 400                                   # more than a year ahead
    plus2 = (datetime.utcnow() + timedelta(hours=3)).replace(microsecond=0)
    local = (plus2 + timedelta(hours=2)).isoformat() + "+02:00"                                                                   # the same moment written with an offset
    assert authed.patch(url, json={"start_at": local}).json()["start_at"] == plus2.isoformat()
    free = _queue(authed, farm)
    assert "printer" in authed.patch(f"/api/queue/{free['id']}", json={"start_at": _when(30)}).json()["detail"]
    authed.patch(url, json={"status": "printing"})
    assert authed.patch(url, json={"start_at": _when(30)}).status_code == 409
    authed.patch(url, json={"status": "queued"})


def test_nothing_starts_unless_the_setting_is_on(authed, farm):
    fake, a, b, m, _ = farm
    item = _ready(authed, farm)
    authed.patch(f"/api/queue/{item['id']}", json={"start_at": _when(30)})
    with Session(engine) as s:
        session_item = s.get(QueueItem, item["id"])
        session_item.start_at = datetime.utcnow() - timedelta(minutes=1)
        s.add(session_item)
        s.commit()
        assert start_at.scheduled(s) == [] and s.get(QueueItem, item["id"]).status == "queued"
    assert not any(c for c in fake.requests if "upload" in c[1])


def _due(item_id, minutes_ago=1):
    with Session(engine) as s:
        row = s.get(QueueItem, item_id)
        row.start_at = datetime.utcnow() - timedelta(minutes=minutes_ago)
        s.add(row)
        s.commit()


def test_a_print_whose_time_has_come_is_sent_and_started_with_the_usual_checks(authed, farm):
    fake, a, b, m, _ = farm
    authed.put("/api/settings", json={"scheduled_starts": "true"})
    item = _ready(authed, farm)
    authed.patch(f"/api/queue/{item['id']}", json={"start_at": _when(30)})
    with Session(engine) as s:
        assert start_at.scheduled(s, now=datetime.utcnow()) == []                                                                # not yet
    _due(item["id"])
    with Session(engine) as s:
        done = start_at.scheduled(s)
    assert len(done) == 1 and done[0].startswith("pf_")
    row = authed.get("/api/queue").json()
    mine = next(q for q in row if q["id"] == item["id"])
    assert mine["status"] == "printing" and mine["start_at"] is None and mine["start_note"] is None
    assert any(c[1] == "/server/files/upload" for c in fake.requests)
    assert any("Started the scheduled print" in x["summary"] and x["actor"] == "Model Hub" for x in authed.get("/api/activity").json()["items"])


def test_a_busy_or_held_or_out_of_service_printer_makes_it_wait_then_give_up(authed, farm, monkeypatch):
    fake, a, b, m, _ = farm
    told = []
    from app import notify
    monkeypatch.setattr(start_at, "notify_event", lambda session, event, title, message, **kw: told.append((event, title, message)))
    authed.put("/api/settings", json={"scheduled_starts": "true"})
    item = _ready(authed, farm)
    authed.patch(f"/api/queue/{item['id']}", json={"start_at": _when(30)})
    _due(item["id"], 1)
    fake.moonraker_state = "printing"                                                                                           # busy
    with Session(engine) as s:
        assert start_at.scheduled(s) == []
    got = next(q for q in authed.get("/api/queue").json() if q["id"] == item["id"])
    assert got["status"] == "queued" and got["start_at"] and "Waiting to start" in got["start_note"] and "busy" in got["start_note"]
    with Session(engine) as s:
        start_at.scheduled(s)
    assert len(told) == 1 and told[0][0] == "schedule_blocked" and "keep trying" in told[0][2]                                 # told once only
    fake.moonraker_state = "standby"
    authed.patch(f"/api/printers/{a['id']}", json={"out_of_service": True})
    with Session(engine) as s:
        assert start_at.scheduled(s) == []
    assert "out of service" in next(q for q in authed.get("/api/queue").json() if q["id"] == item["id"])["start_note"]
    authed.patch(f"/api/printers/{a['id']}", json={"out_of_service": False})
    authed.patch(f"/api/queue/{item['id']}", json={"held": True})
    with Session(engine) as s:
        assert start_at.scheduled(s) == []
    authed.patch(f"/api/queue/{item['id']}", json={"held": False})
    _due(item["id"], 150)                                                                                                       # two and a half hours late
    fake.moonraker_state = "printing"
    with Session(engine) as s:
        assert start_at.scheduled(s) == []
    gave_up = next(q for q in authed.get("/api/queue").json() if q["id"] == item["id"])
    assert gave_up["start_at"] is None and "given up" in gave_up["start_note"] and gave_up["status"] == "queued"
    assert told[-1][0] == "schedule_blocked" and "gave up" in told[-1][2]
    with Session(engine) as s:
        assert start_at.scheduled(s) == []                                                                                      # it is not tried again


def test_the_plate_wait_and_the_stagger_rules_apply_to_a_scheduled_start(authed, farm):
    fake, a, b, m, _ = farm
    authed.put("/api/settings", json={"scheduled_starts": "true", "plate_clear_gate": "true"})
    item = _ready(authed, farm)
    printwatch.reset()
    fake.moonraker_state = "printing"
    with Session(engine) as s:
        printwatch.poll(s)
    fake.moonraker_state = "complete"
    with Session(engine) as s:
        printwatch.poll(s)
    fake.moonraker_state = "standby"
    authed.patch(f"/api/queue/{item['id']}", json={"start_at": _when(30)})
    _due(item["id"])
    with Session(engine) as s:
        assert start_at.scheduled(s) == []
    assert "plate" in next(q for q in authed.get("/api/queue").json() if q["id"] == item["id"])["start_note"]
    authed.post(f"/api/printers/{a['id']}/plate-cleared")
    with Session(engine) as s:
        assert len(start_at.scheduled(s)) == 1


def test_a_failure_of_one_print_does_not_stop_the_next(authed, farm):
    fake, a, b, m, _ = farm
    authed.put("/api/settings", json={"scheduled_starts": "true"})
    first = _ready(authed, farm)
    second = _queue(authed, farm, printer_id=b["id"])
    for item in (first, second):
        authed.patch(f"/api/queue/{item['id']}", json={"start_at": _when(30)})
        _due(item["id"])
    authed.delete(f"/api/printers/{b['id']}")                                                                                   # the second one's printer is gone
    with Session(engine) as s:
        assert len(start_at.scheduled(s)) == 1


# ---------------------------------------------------------------- access groups
def test_groups_are_made_checked_and_removed(authed, rich):
    p = _printer(authed)
    r = authed.post("/api/access-groups", json={"name": "Helpers", "deny": ["orders", "money"], "printers": [p["id"]]})
    assert r.status_code == 200 and r.json() == {"id": r.json()["id"], "name": "Helpers", "deny": ["money", "orders"], "printers": [p["id"]], "members": 0}
    gid = r.json()["id"]
    assert authed.post("/api/access-groups", json={"name": "Helpers"}).status_code == 409
    for bad in ({"name": ""}, {"name": "x" * 50}, {"name": "<b>"}, {"name": "Ok", "deny": ["everything"]}, {"name": "Ok", "deny": "orders"}, {"name": "Ok", "printers": [999999]},
                {"name": "Ok", "printers": ["1"]}, {"name": "Ok", "printers": [True]}):
        assert authed.post("/api/access-groups", json=bad).status_code == 400, bad
    listing = authed.get("/api/access-groups").json()
    assert listing["groups"][0]["name"] == "Helpers" and set(listing["areas"]) == {"library_write", "queue", "orders", "money", "control"} and listing["printers"] == [{"id": p["id"], "name": "Rich"}]
    assert authed.patch(f"/api/access-groups/{gid}", json={"printers": []}).json()["printers"] is None
    authed.post("/api/users", json={"username": "grouped", "password": PW, "role": "member"})
    uid = next(u["id"] for u in authed.get("/api/users").json()["users"] if u["username"] == "grouped")
    assert authed.patch(f"/api/users/{uid}", json={"group_id": 999999}).status_code == 400 and authed.patch(f"/api/users/{uid}", json={"group_id": True}).status_code == 400
    assert authed.patch(f"/api/users/{uid}", json={"group_id": gid}).json()["group_id"] == gid
    assert authed.get("/api/access-groups").json()["groups"][0]["members"] == 1
    assert authed.delete(f"/api/access-groups/{gid}").json() == {"status": "deleted"}
    assert authed.get("/api/users").json()["users"][0]["group_id"] is None                                                        # a later group must not catch this login
    assert authed.delete(f"/api/access-groups/{gid}").status_code == 404 and authed.patch(f"/api/access-groups/{gid}", json={"name": "x"}).status_code == 404


def test_a_group_takes_away_areas_and_never_adds(authed, rich):
    g = authed.post("/api/access-groups", json={"name": "Narrow", "deny": ["orders", "money", "queue", "library_write"]}).json()
    authed.post("/api/users", json={"username": "narrowmem", "password": PW, "role": "member"})
    uid = next(u["id"] for u in authed.get("/api/users").json()["users"] if u["username"] == "narrowmem")
    c = _login("narrowmem")
    assert c.get("/api/orders").status_code == 200 and c.get("/api/queue").status_code == 200                                      # no group yet
    authed.patch(f"/api/users/{uid}", json={"group_id": g["id"]})
    for path in ("/api/orders", "/api/stock", "/api/budgets", "/api/costs/defaults", "/api/analytics", "/api/queue", "/api/calendar"):
        r = c.get(path)
        assert r.status_code == 403 and r.json()["detail"].startswith("Your group may not"), path
    assert c.get("/api/library/models").status_code == 200                                                                         # reading the library is still allowed
    assert c.post("/api/tags", json={"name": "x"}).status_code == 403 and "library" in c.post("/api/tags", json={"name": "x"}).json()["detail"].lower()
    assert c.get("/api/auth/me").json()["deny"] == ["library_write", "money", "orders", "queue"] and c.get("/api/auth/me").json()["group"] == "Narrow"
    assert c.get("/api/settings").status_code == 403                                                                               # the role's own limits are untouched
    authed.patch(f"/api/access-groups/{g['id']}", json={"deny": []})
    assert c.get("/api/orders").status_code == 200                                                                                 # changes apply at once
    authed.patch(f"/api/access-groups/{g['id']}", json={"deny": ["library_write"]})
    viewer = authed.post("/api/users", json={"username": "viewgrp", "password": PW, "role": "viewer"}).json()
    authed.patch(f"/api/users/{viewer['id']}", json={"group_id": g["id"]})
    v = _login("viewgrp")
    assert v.post("/api/library/import", files={"file": ("x.stl", b"solid x", "application/octet-stream")}).status_code == 403          # a viewer is still read-only
    assert authed.get("/api/library/models").status_code == 200                                                                    # the administrator has no group at all


def test_a_group_can_keep_a_printer_login_away_from_starting_and_from_some_printers(authed, farm, monkeypatch):
    from test_control_fit_offsite import Controllable
    _, a, b, m, _ = farm
    fake = Controllable()                                                         # one that also takes pause commands
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(fake), timeout=timeout))
    _keep(authed, m)
    authed.post("/api/users", json={"username": "printgrp", "password": PW, "role": "printer"})
    uid = next(u["id"] for u in authed.get("/api/users").json()["users"] if u["username"] == "printgrp")
    only_a = authed.post("/api/access-groups", json={"name": "Only Alpha", "deny": [], "printers": [a["id"]]}).json()
    authed.patch(f"/api/users/{uid}", json={"group_id": only_a["id"]})
    c = _login("printgrp")
    assert [p["name"] for p in c.get("/api/printers").json()["printers"]] == ["Alpha"]
    assert len(authed.get("/api/printers").json()["printers"]) >= 2
    fake.moonraker_state = "printing"
    assert c.post(f"/api/printers/{a['id']}/control", json={"action": "pause"}).status_code == 200
    r = c.post(f"/api/printers/{b['id']}/control", json={"action": "pause"})
    assert r.status_code == 403 and "that printer" in r.json()["detail"]
    assert c.get(f"/api/printers/{b['id']}/status").status_code == 403 and c.get(f"/api/printers/{a['id']}/status").status_code == 200
    bulk = c.post("/api/printers/bulk-control", json={"action": "pause", "printer_ids": [a["id"], b["id"]]}).json()
    assert [r["name"] for r in bulk["results"]] == ["Alpha"]
    on_b = _queue(authed, farm, printer_id=b["id"])
    fake.moonraker_state = "standby"
    assert c.post(f"/api/queue/{on_b['id']}/send", json={"start": False}).status_code == 403
    on_a = _queue(authed, farm, printer_id=a["id"])
    assert c.post(f"/api/queue/{on_a['id']}/send", json={"start": False}).status_code == 200
    authed.patch(f"/api/access-groups/{only_a['id']}", json={"deny": ["control"]})
    assert "start, pause" in c.post(f"/api/printers/{a['id']}/control", json={"action": "pause"}).json()["detail"]
    assert c.post(f"/api/queue/{on_a['id']}/send", json={"start": False}).status_code == 403
    assert c.get("/api/printers").status_code == 200                                                                               # looking is still fine
    for u in authed.get("/api/users").json()["users"]:
        authed.delete(f"/api/users/{u['id']}")
    authed.delete(f"/api/access-groups/{only_a['id']}")


def test_group_info_survives_a_missing_group_and_bad_json(authed, rich):
    with Session(engine) as s:
        assert auth.group_info(s, AppUser(username="x", password_hash="!", group_id=None)) == {} and auth.group_info(s, AppUser(username="x", password_hash="!", group_id=424242)) == {}
        s.add(AccessGroup(name="Broken", deny_json="not json", printers_json="[1"))
        s.commit()
        row = s.query(AccessGroup).filter(AccessGroup.name == "Broken").one()
        assert auth.group_info(s, AppUser(username="x", password_hash="!", group_id=row.id)) == {"group": "Broken", "deny": [], "printers": None}
        s.delete(row)
        s.commit()
    assert auth.printer_allowed(None, 5) and auth.printer_allowed({"printers": None}, 5) and auth.printer_allowed({"printers": [5]}, 5) and not auth.printer_allowed({"printers": [4]}, 5)


# ---------------------------------------------------------------- reprint and compare
def _log(authed, farm_fixture, **fields):
    fake, a, b, m, _ = farm_fixture
    r = authed.post("/api/prints", json={"model_id": m["id"], **fields})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_past_print_can_be_queued_again_on_another_printer(authed, farm):
    fake, a, b, m, _ = farm
    log = _log(authed, farm, grams=12.5, minutes=95, notes="first run", printed_at="2026-09-01")
    with Session(engine) as s:
        row = s.get(PrintLog, log["id"])
        row.printer_id = a["id"]
        row.measured = True
        s.add(row)
        s.commit()
    same = authed.post(f"/api/queue/reprint/{log['id']}", json={}).json()
    assert same["model_id"] == m["id"] and same["printer_id"] == a["id"] and same["estimated_grams"] == 12.5 and same["estimated_minutes"] == 95 and same["estimate_basis"] == "history"
    assert "2026-09-01" in same["notes"] and same["status"] == "queued"
    other = authed.post(f"/api/queue/reprint/{log['id']}", json={"printer_id": b["id"]}).json()
    assert other["printer_id"] == b["id"] and other["position"] == same["position"] + 1
    assert authed.post(f"/api/queue/reprint/{log['id']}", json={"printer_id": 999999}).status_code == 400 and authed.post(f"/api/queue/reprint/{log['id']}", json={"printer_id": True}).status_code == 400
    assert authed.post("/api/queue/reprint/999999", json={}).status_code == 404
    for q in (same, other):
        authed.delete(f"/api/queue/{q['id']}")
    authed.delete(f"/api/prints/{log['id']}")


def test_two_prints_can_be_compared(authed, farm):
    fake, a, b, m, _ = farm
    first = _log(authed, farm, grams=10, minutes=60, rating=4, printed_at="2026-09-01")
    second = _log(authed, farm, grams=14, minutes=60, outcome="failed", failure_reason="clog", printed_at="2026-09-02")
    r = authed.get("/api/prints/compare", params={"a": first["id"], "b": second["id"]}).json()
    by = {f["field"]: f for f in r["fields"]}
    assert by["model_filename"]["same"] is True and by["minutes"]["same"] is True and by["grams"] == {"field": "grams", "label": "Grams", "a": 10.0, "b": 14.0, "same": False}
    assert by["outcome"]["a"] == "worked" and by["outcome"]["b"].startswith("failed") and by["outcome"]["same"] is False and by["rating"]["a"] == 4 and by["rating"]["b"] is None
    assert r["a"]["id"] == first["id"] and r["b"]["has_photo"] is False
    assert authed.get("/api/prints/compare", params={"a": first["id"], "b": 999999}).status_code == 404 and authed.get("/api/prints/compare", params={"a": 1}).status_code == 422
    for log in (first, second):
        authed.delete(f"/api/prints/{log['id']}")


# ---------------------------------------------------------------- the page
def test_the_controls_are_in_the_page_and_the_themes_are_defined():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html, js, css = (root / "index.html").read_text(encoding="utf-8"), (root / "app.js").read_text(encoding="utf-8"), (root / "style.css").read_text(encoding="utf-8")
    for element in ("scheduled-starts", "groups-box", "theme-select", "accent-select", "shortcuts-help", "compare-modal"):
        assert f'id="{element}"' in html, element
    assert "modelhub_theme" in html and 'data-theme="light"' in css and 'data-accent="green"' in css
    for needle in ("/adjust", "/skip-object", "/objects", "start_at", "/api/queue/reprint/", "/api/prints/compare", "/api/access-groups", "SHORTCUTS", "ctl-set", "queue-start"):
        assert needle in js, needle
