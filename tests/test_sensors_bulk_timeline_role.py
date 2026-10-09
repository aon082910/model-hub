"""Sensor holds (Home Assistant), bulk printer actions, the queue timeline, the printer role and the most-used sort."""
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import Session
from starlette.testclient import TestClient

from app import printwatch, sensors, timeline
from app.db import engine
from test_control_fit_offsite import Controllable
from test_print_farm import _keep, _queue, _stl
from app import printers as printing

PW = "a long enough password"


@pytest.fixture()
def farm(authed, monkeypatch):
    """The print farm fixture of test_print_farm, but with printers that also take pause, resume and cancel."""
    fake = Controllable()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(fake), timeout=timeout))
    printwatch.reset()
    a = authed.post("/api/printers", json={"name": "Alpha", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    b = authed.post("/api/printers", json={"name": "Bravo", "kind": "moonraker", "url": "http://klipper.local:7126"}).json()
    m = authed.post("/api/library/import", files={"file": (f"cf_{uuid.uuid4().hex[:6]}.stl", _stl(40 + uuid.uuid4().int % 90), "application/octet-stream")}).json()
    queued = []
    yield fake, a, b, m, queued
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")
    authed.delete(f"/api/library/models/{m['id']}")
    for p in (a, b):
        authed.delete(f"/api/printers/{p['id']}")


class FakeHA:
    def __init__(self):
        self.states = {"binary_sensor.door": "off", "sensor.chamber": "35.5"}
        self.token_ok = True
        self.status = 200
        self.requests = []

    def __call__(self, request):
        self.requests.append((request.url.path, request.headers.get("authorization")))
        if self.status != 200:
            return httpx.Response(self.status)
        if request.headers.get("authorization") != "Bearer ha-test-token" or not self.token_ok:
            return httpx.Response(401)
        entity = request.url.path.rsplit("/", 1)[-1]
        if request.url.path.startswith("/api/states/") and entity in self.states:
            return httpx.Response(200, json={"entity_id": entity, "state": self.states[entity]})
        return httpx.Response(404)


@pytest.fixture()
def ha(authed, monkeypatch):
    fake = FakeHA()
    monkeypatch.setattr(sensors, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    sensors.reset()
    authed.put("/api/settings", json={"ha_url": "http://homeassistant.local:8123", "ha_token": "ha-test-token"})
    yield fake
    sensors.reset()
    authed.put("/api/settings", json={"ha_url": "", "ha_token": ""})
    for s in authed.get("/api/sensors").json()["sensors"]:
        authed.delete(f"/api/sensors/{s['id']}")


# ---------------------------------------------------------------- sensors
def test_a_sensor_can_be_read_checked_and_the_token_is_kept_secret(authed, ha):
    assert authed.post("/api/sensors/test", json={"entity_id": "binary_sensor.door"}).json() == {"state": "off"}
    assert ha.requests[-1] == ("/api/states/binary_sensor.door", "Bearer ha-test-token")
    assert authed.post("/api/sensors/test", json={"entity_id": "sensor.nothing"}).status_code == 502
    assert authed.post("/api/sensors/test", json={"entity_id": "not an entity"}).status_code == 502
    ha.token_ok = False
    assert "refused the token" in authed.post("/api/sensors/test", json={"entity_id": "binary_sensor.door"}).json()["detail"]
    assert authed.get("/api/settings").json()["ha_token"] == "********"                      # never echoed back
    backup = authed.get("/api/backup")
    assert b"ha-test-token" not in backup.content
    authed.put("/api/settings", json={"ha_url": "http://user:pw@host:8123"})
    assert "address" in authed.post("/api/sensors/test", json={"entity_id": "binary_sensor.door"}).json()["detail"]


def test_conditions_and_unreadable_values():
    ev = sensors.evaluate
    assert ev("on", None, "on") is True and ev("on", None, "off") is False and ev("off", None, "off") is True
    assert ev("above", 40, "45.2") is True and ev("above", 40, "35") is False and ev("below", 10, "5") is True and ev("below", 10, "12") is False
    assert ev("on", None, "unavailable") is None and ev("above", 40, "unknown") is None and ev("above", 40, "warm") is None and ev("above", None, "5") is None


def test_binding_a_sensor_is_checked(authed, farm, ha):
    fake, a, b, m, _ = farm
    good = authed.post("/api/sensors", json={"printer_id": a["id"], "entity_id": "binary_sensor.door", "condition": "on", "label": "Enclosure door"})
    assert good.status_code == 200 and good.json()["printer"] == "Alpha" and good.json()["threshold"] is None
    for bad in ({"printer_id": 987654, "entity_id": "binary_sensor.door", "condition": "on"}, {"printer_id": a["id"], "entity_id": "Bad Name", "condition": "on"},
                {"printer_id": a["id"], "entity_id": "sensor.x", "condition": "sideways"}, {"printer_id": a["id"], "entity_id": "sensor.x", "condition": "above"},
                {"printer_id": a["id"], "entity_id": "sensor.x", "condition": "above", "threshold": "hot"}, {"printer_id": a["id"], "entity_id": "sensor.x", "condition": "on", "label": 5}):
        assert authed.post("/api/sensors", json=bad).status_code == 400, bad
    assert authed.get("/api/sensors").json()["configured"] is True and len(authed.get("/api/sensors").json()["sensors"]) == 1


def test_an_alerting_sensor_holds_its_printer_and_a_broken_one_holds_nothing(authed, farm, ha):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    authed.post("/api/sensors", json={"printer_id": a["id"], "entity_id": "binary_sensor.door", "condition": "on", "label": "Enclosure door"})
    authed.post("/api/sensors", json={"printer_id": b["id"], "entity_id": "sensor.chamber", "condition": "above", "threshold": 40})
    assert authed.post("/api/sensors/poll").json() == {"holding": []} and authed.get("/api/queue/holds").json() == {}
    ha.states["binary_sensor.door"] = "on"
    assert authed.post("/api/sensors/poll").json() == {"holding": [a["id"]]}
    assert authed.get("/api/queue/holds").json() == {str(a["id"]): "Enclosure door is on"}
    item_a = _queue(authed, farm, printer_id=a["id"])
    item_free = _queue(authed, farm)
    assert [c["name"] for c in authed.get("/api/queue/suggestions").json()[str(item_free["id"])]] == ["Bravo"]            # the held printer is not suggested
    fake.moonraker_state = "standby"
    r = authed.post(f"/api/queue/{item_a['id']}/send", json={"start": True})
    assert r.status_code == 409 and r.json()["detail"].startswith("Wait: ") and "Enclosure door is on" in r.json()["detail"]
    forced = authed.post(f"/api/queue/{item_a['id']}/send", json={"start": True, "force": True})
    assert "Wait:" not in forced.text                                                                                      # you can still start anyway
    ha.status = 500                                                                                                          # Home Assistant down: nothing is held
    authed.post("/api/sensors/poll")
    assert authed.get("/api/queue/holds").json() == {}
    row = next(s for s in authed.get("/api/sensors").json()["sensors"] if s["entity_id"] == "binary_sensor.door")
    assert row["alerting"] is None and "error" in row and row["error"]
    ha.status = 200
    ha.states["binary_sensor.door"] = "unavailable"
    authed.post("/api/sensors/poll")
    assert authed.get("/api/queue/holds").json() == {}                                                                      # an unavailable sensor holds nothing
    ha.states["sensor.chamber"] = "52"
    authed.post("/api/sensors/poll")
    assert authed.get("/api/queue/holds").json() == {str(b["id"]): "sensor.chamber is 52"}


def test_removing_a_printer_or_a_sensor_forgets_its_holds(authed, farm, ha):
    fake, a, b, m, _ = farm
    s = authed.post("/api/sensors", json={"printer_id": a["id"], "entity_id": "binary_sensor.door", "condition": "on"}).json()
    ha.states["binary_sensor.door"] = "on"
    authed.post("/api/sensors/poll")
    assert authed.delete(f"/api/sensors/{s['id']}").status_code == 200 and authed.delete(f"/api/sensors/{s['id']}").status_code == 404
    assert authed.get("/api/queue/holds").json() == {}
    authed.post("/api/sensors", json={"printer_id": b["id"], "entity_id": "binary_sensor.door", "condition": "on"})
    authed.delete(f"/api/printers/{b['id']}")
    assert authed.get("/api/sensors").json()["sensors"] == []


def test_the_scheduler_polls_the_sensors_and_only_the_administrator_may_use_them(authed, ha):
    from app import scheduler
    from app.main import app
    assert "sensors" in scheduler._jobs() and scheduler.INTERVALS["sensors"] == 30
    authed.post("/api/users", json={"username": "sensmember", "password": PW, "role": "member"})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "sensmember", "password": PW})
        assert member.get("/api/sensors").status_code == 403 and member.post("/api/sensors/poll").status_code == 403
        assert member.get("/api/queue/holds").status_code == 200                                                            # the reason is shown to everyone
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


# ---------------------------------------------------------------- bulk printer actions
def test_pause_resume_and_cancel_go_to_several_printers_each_checked_on_its_own(authed, farm):
    fake, a, b, m, _ = farm
    fake.moonraker_state = "printing"
    r = authed.post("/api/printers/bulk-control", json={"action": "pause", "printer_ids": [a["id"], b["id"]]}).json()
    assert r["sent"] == 2 and all(x["ok"] for x in r["results"]) and {x["name"] for x in r["results"]} == {"Alpha", "Bravo"}
    assert fake.commands == [("moonraker", "pause"), ("moonraker", "pause")]
    fake.moonraker_state = "standby"
    idle = authed.post("/api/printers/bulk-control", json={"action": "pause", "printer_ids": [a["id"]]}).json()
    assert idle["sent"] == 0 and "cannot be told to pause" in idle["results"][0]["message"]                                  # an idle printer is left alone
    fake.moonraker_state = "paused"
    assert authed.post("/api/printers/bulk-control", json={"action": "resume", "printer_ids": [a["id"], b["id"]]}).json()["sent"] == 2


def test_printers_can_be_picked_by_state_or_tag_and_bad_requests_are_refused(authed, farm):
    fake, a, b, m, _ = farm
    authed.patch(f"/api/printers/{b['id']}", json={"tags": "garage"})
    fake.moonraker_state = "printing"
    printwatch.latest[a["id"]] = {"state": "printing"}
    printwatch.latest[b["id"]] = {"state": "idle"}
    by_state = authed.post("/api/printers/bulk-control", json={"action": "pause", "state": "printing"}).json()
    assert [x["name"] for x in by_state["results"]] == ["Alpha"]
    by_tag = authed.post("/api/printers/bulk-control", json={"action": "pause", "tag": "Garage"}).json()
    assert [x["name"] for x in by_tag["results"]] == ["Bravo"]
    both = authed.post("/api/printers/bulk-control", json={"action": "pause", "state": "printing", "tag": "garage"}).json()
    assert {x["name"] for x in both["results"]} == {"Alpha", "Bravo"}
    for bad in ({"action": "explode", "printer_ids": [a["id"]]}, {"action": "pause"}, {"action": "pause", "printer_ids": "all"}, {"action": "pause", "printer_ids": [True]},
                {"action": "pause", "state": "idle"}, {"action": "pause", "tag": 5}):
        assert authed.post("/api/printers/bulk-control", json=bad).status_code == 400, bad
    assert authed.post("/api/printers/bulk-control", json={"action": "pause", "printer_ids": [987654]}).json() == {"action": "pause", "results": [], "sent": 0}


def test_an_unreachable_printer_does_not_stop_the_others(authed, farm, monkeypatch):
    fake, a, b, m, _ = farm
    fake.moonraker_state = "printing"
    original = fake.__call__

    def flaky(request):
        if request.url.port == 7125:
            raise httpx.ConnectError("down")
        return original(request)
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(flaky), timeout=timeout))
    r = authed.post("/api/printers/bulk-control", json={"action": "cancel", "printer_ids": [a["id"], b["id"]]}).json()
    by = {x["name"]: x for x in r["results"]}
    assert by["Alpha"]["ok"] is False and by["Bravo"]["ok"] is True and r["sent"] == 1


# ---------------------------------------------------------------- the queue timeline
def test_waiting_prints_follow_each_other_on_their_printer(authed, farm):
    fake, a, b, m, _ = farm
    now = datetime(2026, 10, 9, 12, 0, 0)
    first = _queue(authed, farm, printer_id=a["id"], estimated_minutes=60)
    second = _queue(authed, farm, printer_id=a["id"], estimated_minutes=30)
    unknown = _queue(authed, farm, printer_id=a["id"])
    held = _queue(authed, farm, printer_id=a["id"], estimated_minutes=500, held=True)
    third = _queue(authed, farm, printer_id=a["id"], estimated_minutes=15)
    with Session(engine) as s:
        data = timeline.build(s, now)
    row = next(p for p in data["printers"] if p["id"] == a["id"])
    by = {i["id"]: i for i in row["items"]}
    assert by[first["id"]]["start"] == "2026-10-09T12:00:00Z" and by[first["id"]]["end"] == "2026-10-09T13:00:00Z"
    assert by[second["id"]]["start"] == "2026-10-09T13:00:00Z" and by[second["id"]]["end"] == "2026-10-09T13:30:00Z"
    assert by[unknown["id"]]["start"] is None and by[held["id"]]["start"] is None and by[held["id"]]["held"] is True        # neither takes time in the plan
    assert by[third["id"]]["start"] == "2026-10-09T13:30:00Z" and row["free_at"] == "2026-10-09T13:45:00Z"


def test_a_running_print_takes_what_is_left_and_idle_printers_give_a_finish_if_started_now(authed, farm):
    fake, a, b, m, _ = farm
    now = datetime(2026, 10, 9, 12, 0, 0)
    running = _queue(authed, farm, printer_id=a["id"], estimated_minutes=100)
    authed.patch(f"/api/queue/{running['id']}", json={"status": "printing"})
    behind = _queue(authed, farm, printer_id=a["id"], estimated_minutes=20)
    ready = _queue(authed, farm, printer_id=b["id"], estimated_minutes=45)
    loose = _queue(authed, farm, estimated_minutes=10)
    printwatch.latest[a["id"]] = {"online": True, "state": "printing", "progress": 75}
    printwatch.latest[b["id"]] = {"online": True, "state": "idle", "progress": None}
    with Session(engine) as s:
        data = timeline.build(s, now)
    rowa = next(p for p in data["printers"] if p["id"] == a["id"])
    by = {i["id"]: i for i in rowa["items"]}
    assert by[running["id"]]["end"] == "2026-10-09T12:25:00Z"                                                                 # 25 of 100 minutes left
    assert by[behind["id"]]["start"] == "2026-10-09T12:25:00Z" and by[behind["id"]]["end"] == "2026-10-09T12:45:00Z"
    assert data["if_started_now"][str(ready["id"])] == "2026-10-09T12:45:00Z"                                               # its printer is idle
    assert str(behind["id"]) not in data["if_started_now"]                                                                   # its printer is busy
    assert data["if_started_now"][str(loose["id"])] == "2026-10-09T12:10:00Z"                                               # unassigned, and a printer is idle
    assert [i["id"] for i in data["unassigned"]] == [loose["id"]]


def test_without_an_idle_printer_nothing_can_start_now(authed, farm):
    fake, a, b, m, _ = farm
    loose = _queue(authed, farm, estimated_minutes=10)
    printwatch.latest[a["id"]] = {"online": True, "state": "printing"}
    printwatch.latest[b["id"]] = {"online": False, "state": "offline"}
    with Session(engine) as s:
        assert str(loose["id"]) not in timeline.build(s)["if_started_now"]
    assert authed.get("/api/queue/timeline").status_code == 200 and "now" in authed.get("/api/queue/timeline").json()


# ---------------------------------------------------------------- the printer role
@pytest.fixture()
def printer_login(authed):
    from app.main import app
    authed.post("/api/users", json={"username": "operator", "password": PW, "role": "printer"})
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": "operator", "password": PW}).status_code == 200
    yield client
    for u in authed.get("/api/users").json()["users"]:
        authed.delete(f"/api/users/{u['id']}")


def test_the_printer_role_exists_and_can_look_at_printers_but_not_manage_them(authed, farm, printer_login):
    fake, a, b, m, _ = farm
    assert "printer" in authed.get("/api/users").json()["roles"] and printer_login.get("/api/auth/me").json()["role"] == "printer"
    assert printer_login.get("/api/printers").status_code == 200 and printer_login.get("/api/library/models").status_code == 200
    assert printer_login.get(f"/api/printers/{a['id']}/status").status_code == 200 and printer_login.get("/api/queue").status_code == 200
    for method, path in (("get", "/api/settings"), ("get", "/api/users"), ("get", "/api/backup"), ("get", "/api/sensors"), ("get", "/api/tokens")):
        assert getattr(printer_login, method)(path).status_code == 403, path
    for method, path, body in (("post", "/api/printers", {"name": "x", "kind": "moonraker", "url": "http://a.local"}), ("patch", f"/api/printers/{a['id']}", {"name": "y"}),
                               ("delete", f"/api/printers/{a['id']}", None), ("post", "/api/queue", {"model_id": m["id"]}), ("delete", f"/api/library/models/{m['id']}", None),
                               ("put", "/api/settings", {"x": "1"}), ("post", f"/api/printers/{a['id']}/plug-test", {})):
        r = getattr(printer_login, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (method, path)


def test_the_printer_role_can_start_pause_resume_and_cancel(authed, farm, printer_login):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    item = _queue(authed, farm, printer_id=a["id"])
    fake.moonraker_state = "standby"
    assert printer_login.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200
    fake.moonraker_state = "printing"
    assert printer_login.post(f"/api/printers/{a['id']}/control", json={"action": "pause"}).status_code == 200
    assert printer_login.post("/api/printers/bulk-control", json={"action": "pause", "printer_ids": [a["id"], b["id"]]}).json()["sent"] == 2
    assert fake.commands.count(("moonraker", "pause")) == 3


def test_the_printer_role_can_change_its_own_password_and_nothing_else_written(authed, printer_login):
    new = "another long password"
    assert printer_login.post("/api/auth/me/password", json={"current_password": PW, "new_password": new}).status_code == 200
    assert printer_login.post("/api/auth/logout").status_code == 200


# ---------------------------------------------------------------- most used first
def _model(c):
    n = 30 + uuid.uuid4().int % 300
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 5}\n endloop\nendfacet\nendsolid t\n").encode() + uuid.uuid4().hex.encode()
    return c.post("/api/library/import", files={"file": (f"pop_{uuid.uuid4().hex[:6]}.stl", stl, "application/octet-stream")}).json()


def test_the_most_used_models_come_first(authed):
    plain, starred, printed, both = _model(authed), _model(authed), _model(authed), _model(authed)
    try:
        authed.put(f"/api/favorites/{starred['id']}")
        authed.post("/api/prints", json={"model_id": printed["id"], "grams": 5})
        authed.put(f"/api/favorites/{both['id']}")
        authed.post("/api/prints", json={"model_id": both["id"], "grams": 5})
        ids = [x["id"] for x in authed.get("/api/library/models", params={"sort": "popular", "limit": 500}).json()]
        mine = [i for i in ids if i in {plain["id"], starred["id"], printed["id"], both["id"]}]
        assert mine == [both["id"], printed["id"], starred["id"], plain["id"]]                                              # print (3) + star (2), print (3), star (2), nothing
        assert authed.get("/api/library/models", params={"sort": "nonsense"}).status_code == 200
    finally:
        for m in (plain, starred, printed, both):
            authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- the page
def test_the_controls_are_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    for element in ("sensors-box", "ha-url", "ha-token", "queue-view-list", "queue-view-timeline", "queue-timeline", "printers-bulk", "new-user-role"):
        assert f'id="{element}"' in html, element
    assert 'value="popular"' in html and '<option value="printer">printer</option>' in html
    for needle in ("/api/sensors", "/api/printers/bulk-control", "/api/queue/timeline", "/api/queue/holds", "printerRole", "if_started_now", "printer-select"):
        assert needle in js, needle
