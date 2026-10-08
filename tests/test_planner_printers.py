"""The calendar per printer, and warnings when the plan needs more filament than the spools hold."""
import uuid
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from app import plan_watch, scheduler


def _stl():
    n = 30 + uuid.uuid4().int % 250
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"pp_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


@pytest.fixture(autouse=True)
def clean(authed):
    def wipe():
        for q in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{q['id']}")
        for p in authed.get("/api/printers").json()["printers"]:
            authed.delete(f"/api/printers/{p['id']}")
        authed.put("/api/settings", json={"calendar_hours_per_day": "", "plan_short_notified": "", "notify_webhook_url": ""})
    wipe()
    yield
    wipe()


@pytest.fixture()
def two_printers(authed):
    a = authed.post("/api/printers", json={"name": "Alpha", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    b = authed.post("/api/printers", json={"name": "Bravo", "kind": "moonraker", "url": "http://klipper.local:7126"}).json()
    return a, b


def _queue(c, model, minutes=None, **extra):
    r = c.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": minutes, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _month(c, month, **params):
    r = c.get("/api/calendar", params={"month": month, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_calendar_can_be_limited_to_one_printer_or_to_entries_without_one(authed, model, two_printers):
    a, b = two_printers
    on_a = _queue(authed, model, 60, printer_id=a["id"], planned_date="2034-03-01")
    on_b = _queue(authed, model, 30, printer_id=b["id"], planned_date="2034-03-01")
    loose = _queue(authed, model, 20, planned_date="2034-03-02")
    ids = lambda view: sorted(p["id"] for d in view["days"].values() for p in d["planned"])
    assert ids(_month(authed, "2034-03")) == sorted([on_a["id"], on_b["id"], loose["id"]])
    assert ids(_month(authed, "2034-03", printer=a["id"])) == [on_a["id"]]
    assert ids(_month(authed, "2034-03", printer="none")) == [loose["id"]]
    view = _month(authed, "2034-03", printer=b["id"])
    assert view["printer"] == b["id"] and [p["name"] for p in view["printers"]] == ["Alpha", "Bravo"]
    assert view["days"]["2034-03-01"]["minutes"] == 30


def test_what_was_printed_only_shows_when_every_printer_is_shown(authed, model, two_printers):
    a, b = two_printers
    authed.post("/api/prints", json={"model_id": model["id"], "printed_at": "2034-04-05", "minutes": 20, "deduct": False})
    assert _month(authed, "2034-04")["days"]["2034-04-05"]["printed"]
    assert "2034-04-05" not in _month(authed, "2034-04", printer=a["id"])["days"]


def test_bad_printer_choices_are_refused(authed, two_printers):
    for bad in ("abc", "-1", "0", "1.5"):
        assert authed.get("/api/calendar", params={"month": "2034-05", "printer": bad}).status_code == 400
    assert authed.get("/api/calendar", params={"month": "2034-05", "printer": 987654}).status_code == 400
    assert authed.get("/api/calendar/export.ics", params={"printer": "abc"}).status_code == 400
    assert authed.get("/api/calendar", params={"month": "2034-05", "printer": "all"}).status_code == 200


def test_one_printer_cannot_be_given_more_than_its_own_day(authed, model, two_printers):
    a, b = two_printers
    authed.put("/api/settings", json={"calendar_hours_per_day": "1"})
    _queue(authed, model, 90, printer_id=a["id"], planned_date="2034-06-01")             # 90 min on a printer that has 60: too much
    _queue(authed, model, 45, printer_id=b["id"], planned_date="2034-06-02")
    _queue(authed, model, 45, printer_id=a["id"], planned_date="2034-06-02")             # 90 in total, but 45 + 45 on two printers: fine
    view = _month(authed, "2034-06")
    assert view["capacity_minutes"] == 120
    assert view["days"]["2034-06-01"]["overbooked"] is True and view["days"]["2034-06-02"]["overbooked"] is False
    assert _month(authed, "2034-06", printer=a["id"])["capacity_minutes"] == 60


def test_planning_keeps_each_printer_within_its_own_day(authed, model, two_printers):
    a, b = two_printers
    authed.put("/api/settings", json={"calendar_hours_per_day": "1"})
    first = _queue(authed, model, 50, printer_id=a["id"])
    second = _queue(authed, model, 50, printer_id=a["id"])                               # does not fit beside the first on Alpha
    other = _queue(authed, model, 50, printer_id=b["id"])                                # Bravo is free that day
    r = authed.post("/api/calendar/plan", json={"start": "2034-07-01"}).json()
    days = {x["id"]: x["planned_date"] for x in r["assigned"]}
    assert days == {first["id"]: "2034-07-01", second["id"]: "2034-07-02", other["id"]: "2034-07-01"}


def test_an_entry_with_no_printer_uses_what_is_left_of_the_whole_day(authed, model, two_printers):
    a, b = two_printers
    authed.put("/api/settings", json={"calendar_hours_per_day": "1"})
    _queue(authed, model, 60, printer_id=a["id"], planned_date="2034-08-01")
    _queue(authed, model, 60, printer_id=b["id"], planned_date="2034-08-01")             # both printers are full that day
    loose = _queue(authed, model, 30)
    r = authed.post("/api/calendar/plan", json={"start": "2034-08-01"}).json()
    assert r["assigned"][0]["id"] == loose["id"] and r["assigned"][0]["planned_date"] == "2034-08-02"


def test_the_ics_file_can_be_limited_to_one_printer(authed, model, two_printers):
    a, b = two_printers
    one = _queue(authed, model, 60, printer_id=a["id"], planned_date="2034-09-01")
    two = _queue(authed, model, 60, printer_id=b["id"], planned_date="2034-09-01")
    both = authed.get("/api/calendar/export.ics").text
    assert f"queue-{one['id']}@" in both and f"queue-{two['id']}@" in both and "X-WR-CALNAME:Model Hub prints\r\n" in both
    only = authed.get("/api/calendar/export.ics", params={"printer": a["id"]}).text
    assert f"queue-{one['id']}@" in only and f"queue-{two['id']}@" not in only and "(Alpha)" in only


def _spool(c, remaining):
    return c.post("/api/filament", json={"material": "PLA", "brand": "Acme", "color": f"plan-{uuid.uuid4().hex[:4]}", "spool_weight_g": 1000, "remaining_g": remaining}).json()


def test_the_calendar_says_when_a_spool_will_run_out_before_the_plan_is_done(authed, model):
    spool = _spool(authed, 100)
    try:
        first = _queue(authed, model, 60, filament_id=spool["id"], estimated_grams=60, planned_date="2034-10-01")
        second = _queue(authed, model, 60, filament_id=spool["id"], estimated_grams=60, planned_date="2034-10-02")
        third = _queue(authed, model, 60, filament_id=spool["id"], estimated_grams=30, planned_date="2034-10-03")
        view = _month(authed, "2034-10")
        entry = lambda d, i: next(p for p in view["days"][d]["planned"] if p["id"] == i)
        assert entry("2034-10-01", first["id"])["short"] is None
        assert entry("2034-10-02", second["id"])["short"] == {"spool_id": spool["id"], "spool": f"PLA Acme {spool['color']}", "need": 60.0, "short_by": 20.0}
        assert entry("2034-10-03", third["id"])["short"]["short_by"] == 30.0                    # nothing is left at all by then
        assert view["days"]["2034-10-01"]["short"] is False and view["days"]["2034-10-02"]["short"] is True and view["short_count"] == 2
        authed.patch(f"/api/filament/{spool['id']}", json={"remaining_g": 500})                  # restocked
        assert _month(authed, "2034-10")["short_count"] == 0
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_entries_without_a_spool_or_a_weight_are_never_short(authed, model):
    spool = _spool(authed, 0)
    try:
        _queue(authed, model, 60, estimated_grams=500, planned_date="2034-11-01")                    # no spool chosen
        _queue(authed, model, 60, filament_id=spool["id"], planned_date="2034-11-02")                # no weight known
        assert _month(authed, "2034-11")["short_count"] == 0
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_finished_entries_do_not_count_against_the_spool(authed, model):
    spool = _spool(authed, 100)
    try:
        done = _queue(authed, model, 60, filament_id=spool["id"], estimated_grams=90, planned_date="2034-12-01")
        authed.patch(f"/api/queue/{done['id']}", json={"status": "done"})                          # the spool already gave up its 90 g
        _queue(authed, model, 60, filament_id=spool["id"], estimated_grams=5, planned_date="2034-12-02")
        assert _month(authed, "2034-12")["short_count"] == 0
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_a_shortage_in_the_next_two_weeks_is_announced_once(authed, model, monkeypatch):
    sent = []
    monkeypatch.setattr(plan_watch, "notify_event", lambda session, event, title, message: sent.append((event, title, message)))
    spool = _spool(authed, 10)
    soon = (date.today() + timedelta(days=3)).isoformat()
    far = (date.today() + timedelta(days=60)).isoformat()
    try:
        item = _queue(authed, model, 60, filament_id=spool["id"], estimated_grams=50, planned_date=soon)
        _queue(authed, model, 60, filament_id=spool["id"], estimated_grams=500, planned_date=far)           # too far off to nag about
        from sqlmodel import Session
        from app.db import engine
        with Session(engine) as s:
            fresh = plan_watch.check_and_notify(s)
            assert len(fresh) == 1 and "short by 40 g" in fresh[0] and soon in fresh[0]
            assert plan_watch.check_and_notify(s) == []                                                         # once is enough
        assert sent and sent[0][0] == "plan_short" and "short by 40 g" in sent[0][2] and len(sent) == 1
        authed.patch(f"/api/filament/{spool['id']}", json={"remaining_g": 400})
        with Session(engine) as s:
            assert plan_watch.check_and_notify(s) == []                                                         # resolved: forgotten
        authed.patch(f"/api/filament/{spool['id']}", json={"remaining_g": 10})
        with Session(engine) as s:
            assert len(plan_watch.check_and_notify(s)) == 1                                                     # short again: told again
        assert len(sent) == 2
        assert item["id"]
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_the_scheduler_runs_the_plan_check_and_it_can_be_switched_off(authed):
    assert "plan" in scheduler.INTERVALS and "plan" in scheduler._jobs()
    from app.notify import EVENTS
    assert "plan_short" in EVENTS
    assert any(e["id"] == "plan_short" for e in authed.get("/api/settings/notify-events").json()["events"])


def test_viewers_can_look_at_each_printers_calendar(authed, two_printers):
    from app.main import app
    a, b = two_printers
    authed.post("/api/users", json={"username": "ppviewer", "password": "a long enough password", "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "ppviewer", "password": "a long enough password"})
        assert viewer.get("/api/calendar", params={"month": "2034-03", "printer": a["id"]}).status_code == 200
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
