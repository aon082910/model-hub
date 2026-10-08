"""Which spool is loaded in which slot of a printer (an AMS, an MMU...), and queue entries printed from a slot."""
import uuid

import pytest
from starlette.testclient import TestClient

PW = "a long enough password"


def _stl():
    n = 30 + uuid.uuid4().int % 250
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"sl_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


@pytest.fixture(autouse=True)
def clean(authed):
    def wipe():
        for q in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{q['id']}")
        for p in authed.get("/api/printers").json()["printers"]:
            authed.delete(f"/api/printers/{p['id']}")
    wipe()
    yield
    wipe()


@pytest.fixture()
def printer(authed):
    p = authed.post("/api/printers", json={"name": "Multi", "kind": "moonraker", "url": "http://klipper.local:7125", "slot_count": 4}).json()
    return p


def _spool(c, color, grams=500):
    return c.post("/api/filament", json={"material": "PLA", "color": color, "spool_weight_g": 1000, "remaining_g": grams}).json()


def _remaining(c, spool):
    return next(f for f in c.get("/api/filament").json() if f["id"] == spool["id"])["remaining_g"]


def test_a_printer_says_how_many_slots_it_has(authed, printer):
    assert printer["slot_count"] == 4
    plain = authed.post("/api/printers", json={"name": "Plain", "kind": "moonraker", "url": "http://klipper.local:7126"}).json()
    assert plain["slot_count"] == 0
    assert authed.patch(f"/api/printers/{plain['id']}", json={"slot_count": 16}).json()["slot_count"] == 16
    for bad in (17, -1, "x", True, 1.5):
        assert authed.patch(f"/api/printers/{plain['id']}", json={"slot_count": bad}).status_code == 400
    assert authed.patch(f"/api/printers/{plain['id']}", json={"slot_count": None}).json()["slot_count"] == 0


def test_only_printers_with_slots_are_listed_with_their_empty_slots(authed, printer):
    authed.post("/api/printers", json={"name": "Plain", "kind": "moonraker", "url": "http://klipper.local:7126"})
    listed = authed.get("/api/slots").json()["printers"]
    assert [p["name"] for p in listed] == ["Multi"] and [s["slot"] for s in listed[0]["slots"]] == [1, 2, 3, 4]
    assert all(s["spool"] is None for s in listed[0]["slots"])


def test_a_spool_is_loaded_labelled_moved_and_removed(authed, printer):
    red, blue = _spool(authed, "slot-red"), _spool(authed, "slot-blue")
    try:
        r = authed.put(f"/api/slots/{printer['id']}/2", json={"filament_id": red["id"], "label": "AMS A2"})
        assert r.status_code == 200 and r.json()["spool"]["color"] == "slot-red" and r.json()["label"] == "AMS A2"
        authed.put(f"/api/slots/{printer['id']}/3", json={"filament_id": red["id"]})               # a spool is in only one place
        slots = {s["slot"]: s for s in authed.get("/api/slots").json()["printers"][0]["slots"]}
        assert slots[2]["spool"] is None and slots[3]["spool"]["id"] == red["id"] and slots[2]["label"] == "AMS A2"
        authed.put(f"/api/slots/{printer['id']}/3", json={"filament_id": blue["id"]})                # replaced
        assert authed.get("/api/slots").json()["printers"][0]["slots"][2]["spool"]["id"] == blue["id"]
        assert authed.put(f"/api/slots/{printer['id']}/3", json={"filament_id": None}).json()["spool"] is None
    finally:
        for s in (red, blue):
            authed.delete(f"/api/filament/{s['id']}")


def test_bad_loads_are_refused(authed, printer):
    red = _spool(authed, "slot-bad")
    try:
        assert authed.put(f"/api/slots/{printer['id']}/5", json={"filament_id": red["id"]}).status_code == 400          # only 4 slots
        assert authed.put(f"/api/slots/{printer['id']}/0", json={"filament_id": red["id"]}).status_code == 400
        assert authed.put(f"/api/slots/{printer['id']}/1", json={"filament_id": 987654}).status_code == 400
        assert authed.put(f"/api/slots/{printer['id']}/1", json={"filament_id": "1"}).status_code == 400
        assert authed.put(f"/api/slots/{printer['id']}/1", json={"label": 5}).status_code == 400
        assert authed.put("/api/slots/987654/1", json={"filament_id": red["id"]}).status_code == 404
    finally:
        authed.delete(f"/api/filament/{red['id']}")


def test_a_queue_entry_takes_the_spool_in_its_slot(authed, printer, model):
    red = _spool(authed, "slot-q")
    try:
        authed.put(f"/api/slots/{printer['id']}/2", json={"filament_id": red["id"]})
        item = authed.post("/api/queue", json={"model_id": model["id"], "printer_id": printer["id"], "slot": 2, "estimated_grams": 30}).json()
        assert item["slot"] == 2 and item["filament_id"] == red["id"]
        assert authed.post("/api/queue", json={"model_id": model["id"], "slot": 2}).status_code == 400                     # no printer
        assert authed.post("/api/queue", json={"model_id": model["id"], "printer_id": printer["id"], "slot": 9}).status_code == 400
        assert authed.post("/api/queue", json={"model_id": model["id"], "printer_id": printer["id"], "slot": "2"}).status_code == 400
        other = authed.patch(f"/api/queue/{item['id']}", json={"slot": 3}).json()
        assert other["slot"] == 3 and other["filament_id"] == red["id"]                                                   # slot 3 is empty: unchanged
        assert authed.patch(f"/api/queue/{item['id']}", json={"slot": None}).json()["slot"] is None
    finally:
        authed.delete(f"/api/filament/{red['id']}")


def test_changing_the_printer_drops_the_slot(authed, printer, model):
    plain = authed.post("/api/printers", json={"name": "Plain", "kind": "moonraker", "url": "http://klipper.local:7126"}).json()
    item = authed.post("/api/queue", json={"model_id": model["id"], "printer_id": printer["id"], "slot": 1}).json()
    moved = authed.patch(f"/api/queue/{item['id']}", json={"printer_id": plain["id"]}).json()
    assert moved["printer_id"] == plain["id"] and moved["slot"] is None


def test_finishing_counts_the_spool_that_is_in_the_slot_then_not_the_one_first_chosen(authed, printer, model):
    red, blue = _spool(authed, "slot-first", 400), _spool(authed, "slot-swapped", 400)
    try:
        authed.put(f"/api/slots/{printer['id']}/1", json={"filament_id": red["id"]})
        item = authed.post("/api/queue", json={"model_id": model["id"], "printer_id": printer["id"], "slot": 1, "estimated_grams": 50}).json()
        authed.put(f"/api/slots/{printer['id']}/1", json={"filament_id": blue["id"]})                # the spool was swapped before it printed
        authed.patch(f"/api/queue/{item['id']}", json={"status": "done"})
        assert _remaining(authed, red) == 400 and _remaining(authed, blue) == 350
        log = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]
        assert log["filament_id"] == blue["id"] and log["printer_id"] == printer["id"]
    finally:
        for s in (red, blue):
            authed.delete(f"/api/filament/{s['id']}")


def test_the_calendar_checks_the_spool_in_the_slot(authed, printer, model):
    small, big = _spool(authed, "slot-small", 20), _spool(authed, "slot-big", 900)
    try:
        authed.put(f"/api/slots/{printer['id']}/1", json={"filament_id": small["id"]})
        authed.post("/api/queue", json={"model_id": model["id"], "printer_id": printer["id"], "slot": 1, "estimated_grams": 50, "planned_date": "2035-01-05"})
        assert authed.get("/api/calendar", params={"month": "2035-01"}).json()["short_count"] == 1
        authed.put(f"/api/slots/{printer['id']}/1", json={"filament_id": big["id"]})                    # load the big spool and it is fine
        assert authed.get("/api/calendar", params={"month": "2035-01"}).json()["short_count"] == 0
    finally:
        for s in (small, big):
            authed.delete(f"/api/filament/{s['id']}")


def test_removing_a_spool_or_a_printer_leaves_nothing_behind(authed, printer):
    red = _spool(authed, "slot-gone")
    authed.put(f"/api/slots/{printer['id']}/4", json={"filament_id": red["id"]})
    authed.delete(f"/api/filament/{red['id']}")
    assert authed.get("/api/slots").json()["printers"][0]["slots"][3]["spool"] is None
    blue = _spool(authed, "slot-gone-2")
    try:
        authed.put(f"/api/slots/{printer['id']}/1", json={"filament_id": blue["id"]})
        authed.delete(f"/api/printers/{printer['id']}")
        again = authed.post("/api/printers", json={"name": "Reused", "kind": "moonraker", "url": "http://klipper.local:7125", "slot_count": 4}).json()
        assert all(s["spool"] is None for s in authed.get("/api/slots").json()["printers"][0]["slots"])         # even if the id is reused
        assert again["id"]
    finally:
        authed.delete(f"/api/filament/{blue['id']}")


def test_loading_is_recorded_and_viewers_can_only_look(authed, printer):
    from app.main import app
    red = _spool(authed, "slot-act")
    authed.post("/api/users", json={"username": "slotviewer", "password": PW, "role": "viewer"})
    authed.post("/api/users", json={"username": "slotmember", "password": PW, "role": "member"})
    try:
        authed.put(f"/api/slots/{printer['id']}/1", json={"filament_id": red["id"]})
        entries = authed.get("/api/activity", params={"action": "slot"}).json()
        assert any("Multi slot 1" in e["summary"] for e in (entries["items"] if isinstance(entries, dict) else entries))
        viewer, member = TestClient(app), TestClient(app)
        viewer.post("/api/auth/login", json={"username": "slotviewer", "password": PW})
        member.post("/api/auth/login", json={"username": "slotmember", "password": PW})
        assert viewer.get("/api/slots").status_code == 200
        assert viewer.put(f"/api/slots/{printer['id']}/2", json={"filament_id": red["id"]}).status_code == 403
        assert member.put(f"/api/slots/{printer['id']}/2", json={"filament_id": red["id"]}).status_code == 200      # loading a spool is not an admin job
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
        authed.delete(f"/api/filament/{red['id']}")
