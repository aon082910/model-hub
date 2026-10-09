"""Smart-plug energy, filament for a part-printed failure, favourites per login, and collection covers."""
import uuid

import httpx
import pytest
from sqlmodel import Session
from starlette.testclient import TestClient

from app import plugs, printers as printing, printwatch
from app.db import engine
from app.models import PrinterJob
from test_control_fit_offsite import Controllable

PW = "a long enough password"


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 3}\n endloop\nendfacet\nendsolid t\n").encode() + uuid.uuid4().hex.encode()


def _model(c, n=None):
    m = c.post("/api/library/import", files={"file": (f"pf_{uuid.uuid4().hex[:6]}.stl", _stl(n or 30 + uuid.uuid4().int % 300), "application/octet-stream")})
    assert m.status_code == 200, m.text
    return m.json()


class Metered(Controllable):
    """A printer that also answers as a smart plug whose energy total grows between readings."""

    def __init__(self):
        super().__init__()
        self.totals = [10.0, 10.75]
        self.plug_kind = "tasmota"
        self.plug_status = 200

    def __call__(self, request):
        if request.url.host == "plug.local":
            self.requests.append((request.method, request.url.path, None))
            if self.plug_status != 200:
                return httpx.Response(self.plug_status)
            if self.plug_kind == "tasmota" and request.url.path == "/cm":
                value = self.totals.pop(0) if self.totals else self.totals_last
                self.totals_last = value
                return httpx.Response(200, json={"StatusSNS": {"ENERGY": {"Total": value}}})
            if self.plug_kind == "shelly2" and request.url.path == "/rpc/Switch.GetStatus":
                return httpx.Response(200, json={"aenergy": {"total": 12500.0}})
            if self.plug_kind == "shelly1" and request.url.path == "/status":
                return httpx.Response(200, json={"meters": [{"power": 100, "total": 600000}]})
            return httpx.Response(404)
        return super().__call__(request)


@pytest.fixture()
def fake(monkeypatch, authed):
    f = Metered()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    printwatch.reset()
    yield f
    printwatch.reset()
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")
    for s in authed.get("/api/filament").json():
        authed.delete(f"/api/filament/{s['id']}")
    authed.put("/api/settings", json={"failed_deduct": ""})


def _printer(c, **extra):
    r = c.post("/api/printers", json={"name": "Plugged", "kind": "moonraker", "url": "http://klipper.local:7125", **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- smart plugs
def test_the_plug_total_is_read_for_each_make_and_odd_answers_are_refused(fake):
    assert plugs.read_total_kwh("tasmota", "plug.local") == 10.0
    fake.plug_kind = "shelly2"
    assert plugs.read_total_kwh("shelly", "plug.local") == 12.5                       # watt-hours
    fake.plug_kind = "shelly1"
    assert plugs.read_total_kwh("shelly", "plug.local") == 10.0                       # watt-minutes
    with pytest.raises(plugs.PlugError):
        plugs.read_total_kwh("zigbee", "plug.local")
    fake.plug_status = 500
    with pytest.raises(plugs.PlugError):
        plugs.read_total_kwh("tasmota", "plug.local")
    assert plugs.try_total("tasmota", "plug.local") is None and plugs.try_total(None, "plug.local") is None and plugs.try_total("tasmota", None) is None


def test_two_readings_give_what_a_print_used_and_a_reset_counter_gives_nothing():
    assert plugs.used(10.0, 10.75) == 0.75 and plugs.used(None, 5) is None and plugs.used(5, None) is None
    assert plugs.used(50.0, 2.0) is None and plugs.used(0, 9999) is None


def test_the_plug_is_saved_checked_and_can_be_tested(authed, fake):
    p = _printer(authed)
    assert p["plug_kind"] is None and authed.post(f"/api/printers/{p['id']}/plug-test").status_code == 400
    assert authed.patch(f"/api/printers/{p['id']}", json={"plug_kind": "zigbee", "plug_host": "plug.local"}).status_code == 400
    assert authed.patch(f"/api/printers/{p['id']}", json={"plug_kind": "tasmota", "plug_host": "plug local"}).status_code == 400
    assert authed.patch(f"/api/printers/{p['id']}", json={"plug_kind": "tasmota", "plug_host": "http://plug.local/x"}).json()["plug_host"] == "plug.local"
    fake.totals = [3.5]
    assert authed.post(f"/api/printers/{p['id']}/plug-test").json() == {"total_kwh": 3.5}
    fake.plug_status = 500
    assert authed.post(f"/api/printers/{p['id']}/plug-test").status_code == 502
    cleared = authed.patch(f"/api/printers/{p['id']}", json={"plug_kind": "", "plug_host": ""}).json()
    assert cleared["plug_kind"] is None and cleared["plug_host"] is None


def test_a_finished_print_records_what_the_plug_measured(authed, fake):
    m = _model(authed)
    p = _printer(authed, plug_kind="tasmota", plug_host="plug.local")
    try:
        with Session(engine) as s:
            s.add(PrinterJob(printer_id=p["id"], filename="benchy.gcode", model_id=m["id"], started=True))
            s.commit()
            fake.moonraker_state = "printing"
            printwatch.poll(s)                                                       # the print begins: first reading
            fake.moonraker_state = "complete"
            assert printwatch.poll(s) == [("Plugged", "done")]                       # it ends: second reading
        log = authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"][0]
        assert log["energy_kwh"] == 0.75
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_an_unreachable_plug_never_stops_a_print_being_recorded(authed, fake):
    m = _model(authed)
    p = _printer(authed, plug_kind="tasmota", plug_host="plug.local")
    fake.plug_status = 500
    try:
        with Session(engine) as s:
            s.add(PrinterJob(printer_id=p["id"], filename="benchy.gcode", model_id=m["id"], started=True))
            s.commit()
            fake.moonraker_state = "printing"
            printwatch.poll(s)
            fake.moonraker_state = "complete"
            assert printwatch.poll(s) == [("Plugged", "done")]
        log = authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"][0]
        assert log["energy_kwh"] is None and log["outcome"] is None
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- filament for a part-printed failure
def _fail_at(c, fake, setup_progress=True):
    """Queue a model on a spool of 900 g, print it on the fake printer, cancel it part-way; returns (model, spool, queue item)."""
    m = _model(c)
    spool = c.post("/api/filament", json={"material": "PLA", "color": "red", "spool_weight_g": 1000, "remaining_g": 900}).json()
    p = _printer(c)
    item = c.post("/api/queue", json={"model_id": m["id"], "filament_id": spool["id"], "estimated_grams": 100, "printer_id": p["id"]}).json()
    with Session(engine) as s:
        s.add(PrinterJob(printer_id=p["id"], filename="benchy.gcode", model_id=m["id"], started=True))
        s.commit()
        fake.moonraker_state = "printing"
        printwatch.poll(s)
        fake.moonraker_state = "cancelled"
        assert printwatch.poll(s) == [("Plugged", "stopped")]
    return m, spool, item


def test_a_failed_print_takes_its_share_of_filament_off_the_spool(authed, fake):
    m, spool, item = _fail_at(authed, fake)
    try:
        log = authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"][0]
        assert log["outcome"] == "failed" and log["filament_id"] == spool["id"]
        assert 40 <= log["grams"] <= 45 and log["deducted_g"] == log["grams"]               # the printer said about 42% (0.4237)
        left = next(f for f in authed.get("/api/filament").json() if f["id"] == spool["id"])["remaining_g"]
        assert left == pytest.approx(900 - log["grams"], abs=0.01)
        authed.delete(f"/api/prints/{log['id']}")                                          # deleting the entry puts it back
        assert next(f for f in authed.get("/api/filament").json() if f["id"] == spool["id"])["remaining_g"] == pytest.approx(900, abs=0.01)
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_the_switch_in_settings_turns_it_off(authed, fake):
    authed.put("/api/settings", json={"failed_deduct": "false"})
    m, spool, item = _fail_at(authed, fake)
    try:
        log = authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"][0]
        assert log["outcome"] == "failed" and log["grams"] is None and log["deducted_g"] == 0
        assert next(f for f in authed.get("/api/filament").json() if f["id"] == spool["id"])["remaining_g"] == 900
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_no_progress_means_nothing_is_guessed(authed, fake):
    from app.routers.queue import partial_parts
    from app.models import QueueItem
    m = _model(authed)
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "red", "spool_weight_g": 1000, "remaining_g": 900}).json()
    item = authed.post("/api/queue", json={"model_id": m["id"], "filament_id": spool["id"], "estimated_grams": 100}).json()
    try:
        with Session(engine) as s:
            row = s.get(QueueItem, item["id"])
            assert partial_parts(s, row, None) == [] and partial_parts(s, row, 0) == [] and partial_parts(s, row, 100) == [] and partial_parts(s, row, True) == []
            assert partial_parts(s, row, 50) == [(spool["id"], 50.0)]
            assert partial_parts(s, row, 0.2) == []                                         # 0.2 g is not worth a mark
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- favourites
def test_stars_belong_to_each_login_and_filter_the_library(authed):
    from app.main import app
    a, b, c = _model(authed), _model(authed), _model(authed)
    authed.post("/api/users", json={"username": "favmember", "password": PW, "role": "member"})
    try:
        assert authed.put(f"/api/favorites/{a['id']}").json() == {"favorite": True}
        authed.put(f"/api/favorites/{a['id']}")                                             # twice is still once
        authed.put(f"/api/favorites/{c['id']}")
        assert authed.get("/api/favorites").json()["ids"].count(a["id"]) == 1
        starred = {m["id"] for m in authed.get("/api/library/models", params={"favorite": "true", "limit": 500}).json()}
        assert starred == {a["id"], c["id"]}
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "favmember", "password": PW})
        assert member.get("/api/favorites").json() == {"ids": []}                           # not shared with other logins
        member.put(f"/api/favorites/{b['id']}")
        assert [m["id"] for m in member.get("/api/library/models", params={"favorite": "true"}).json()] == [b["id"]]
        assert authed.delete(f"/api/favorites/{a['id']}").json() == {"favorite": False}
        assert a["id"] not in authed.get("/api/favorites").json()["ids"]
        assert authed.put("/api/favorites/999999").status_code == 404
    finally:
        for m in (a, b, c):
            authed.delete(f"/api/library/models/{m['id']}")
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    from app.models import Favorite
    with Session(engine) as s:
        assert not [f for f in s.query(Favorite).all() if f.model_id in (a["id"], b["id"], c["id"]) or f.owner == "favmember"]    # gone with the model and the login


def test_favourites_cannot_be_used_where_there_is_no_one_to_ask(authed):
    m = _model(authed)
    try:
        r = authed.post("/api/bulk", json={"filters": {"favorite": "true"}, "action": "add_tag", "value": "x"})
        assert r.status_code == 400
        sneaky = authed.post("/api/bulk", json={"filters": {"favorite": "true", "_owner": "admin"}, "action": "add_tag", "value": "x"})
        assert sneaky.status_code == 400                                                    # who is asking is never taken from the request
        assert authed.post("/api/saved-searches", json={"name": "stars", "params": {"favorite": "true"}}).status_code in (200, 201)
    finally:
        for s in authed.get("/api/saved-searches").json():
            authed.delete(f"/api/saved-searches/{s['id']}")
        authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- collection covers
def test_a_collection_shows_a_cover_that_can_be_chosen(authed):
    first, second, outside = _model(authed), _model(authed), _model(authed)
    col = authed.post("/api/collections", json={"name": f"Covers {uuid.uuid4().hex[:5]}"}).json()
    try:
        empty = next(c for c in authed.get("/api/collections").json() if c["id"] == col["id"])
        assert empty["models"] == 0 and empty["cover"] is None and empty["cover_model_id"] is None
        for m in (first, second):
            authed.post(f"/api/collections/{col['id']}/models/{m['id']}")
        thumb = lambda m: authed.get(f"/api/library/models/{m['id']}").json()["thumbnail_path"]
        assert thumb(first) and thumb(second) and thumb(first) != thumb(second)
        listed = next(c for c in authed.get("/api/collections").json() if c["id"] == col["id"])
        assert listed["models"] == 2 and listed["cover"] == thumb(first)                    # the first model's picture until one is chosen
        chosen = authed.patch(f"/api/collections/{col['id']}", json={"cover_model_id": second["id"]}).json()
        assert chosen["cover"] == thumb(second) and chosen["cover_model_id"] == second["id"]
        assert authed.patch(f"/api/collections/{col['id']}", json={"cover_model_id": outside["id"]}).status_code == 400
        assert authed.patch(f"/api/collections/{col['id']}", json={"cover_model_id": "7"}).status_code == 400
        assert authed.patch("/api/collections/999999", json={"cover_model_id": None}).status_code == 404
        authed.delete(f"/api/collections/{col['id']}/models/{second['id']}")                # the cover left: back to the first picture
        back = next(c for c in authed.get("/api/collections").json() if c["id"] == col["id"])
        assert back["cover_model_id"] is None and back["cover"] == thumb(first)
        authed.patch(f"/api/collections/{col['id']}", json={"cover_model_id": first["id"]})
        assert authed.patch(f"/api/collections/{col['id']}", json={"cover_model_id": None}).json()["cover_model_id"] is None
    finally:
        authed.delete(f"/api/collections/{col['id']}")
        for m in (first, second, outside):
            authed.delete(f"/api/library/models/{m['id']}")


def test_deleting_the_cover_model_clears_the_choice(authed):
    m = _model(authed)
    col = authed.post("/api/collections", json={"name": f"Gone {uuid.uuid4().hex[:5]}"}).json()
    try:
        authed.post(f"/api/collections/{col['id']}/models/{m['id']}")
        authed.patch(f"/api/collections/{col['id']}", json={"cover_model_id": m["id"]})
        authed.delete(f"/api/library/models/{m['id']}")
        gone = next(c for c in authed.get("/api/collections").json() if c["id"] == col["id"])
        assert gone["cover_model_id"] is None and gone["cover"] is None
    finally:
        authed.delete(f"/api/collections/{col['id']}")
