"""Repeating a week of the plan."""
import uuid

import pytest
from starlette.testclient import TestClient

PW = "a long enough password"
MONDAY = "2036-03-03"                          # a Monday
WEEK = ["2036-03-03", "2036-03-04", "2036-03-05", "2036-03-06", "2036-03-07", "2036-03-08", "2036-03-09"]


def _stl():
    n = 30 + uuid.uuid4().int % 250
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"cw_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
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


def _queue(c, model, day, **extra):
    r = c.post("/api/queue", json={"model_id": model["id"], "planned_date": day, "estimated_minutes": 30, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _all(c):
    return c.get("/api/queue").json()


def test_a_week_is_repeated_on_the_same_weekdays_a_week_later(authed, model):
    printer = authed.post("/api/printers", json={"name": "Copy P", "kind": "moonraker", "url": "http://klipper.local:7125", "slot_count": 2}).json()
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "cw", "spool_weight_g": 1000, "remaining_g": 900}).json()
    try:
        a = _queue(authed, model, "2036-03-03", printer_id=printer["id"], slot=2, filament_id=spool["id"], estimated_grams=40, notes="mon")
        b = _queue(authed, model, "2036-03-05")
        authed.patch(f"/api/queue/{b['id']}", json={"status": "done"})
        outside = _queue(authed, model, "2036-03-10")                                   # the week after: left alone
        r = authed.post("/api/calendar/copy", json={"date": "2036-03-05"}).json()               # any day of the week will do
        assert r["week"] == MONDAY and r["dry_run"] is False
        assert sorted(c["planned_date"] for c in r["created"]) == ["2036-03-10", "2036-03-12"]
        made = {q["id"]: q for q in _all(authed)}
        copy_a = next(made[c["id"]] for c in r["created"] if c["source_id"] == a["id"])
        assert copy_a["status"] == "queued" and copy_a["planned_date"] == "2036-03-10" and copy_a["notes"] == "mon"
        assert (copy_a["printer_id"], copy_a["slot"], copy_a["filament_id"], copy_a["estimated_grams"], copy_a["estimated_minutes"]) \
            == (printer["id"], 2, spool["id"], 40, 30)
        copy_b = next(made[c["id"]] for c in r["created"] if c["source_id"] == b["id"])
        assert copy_b["status"] == "queued" and copy_b["planned_date"] == "2036-03-12"           # a finished print becomes a waiting one again
        assert made[outside["id"]]["planned_date"] == "2036-03-10" and len(made) == 5
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_several_weeks_at_once_and_the_originals_are_untouched(authed, model):
    a = _queue(authed, model, "2036-03-04")
    r = authed.post("/api/calendar/copy", json={"date": "2036-03-04", "weeks": 3}).json()
    assert [c["planned_date"] for c in r["created"]] == ["2036-03-11", "2036-03-18", "2036-03-25"]
    assert next(q for q in _all(authed) if q["id"] == a["id"])["planned_date"] == "2036-03-04"


def test_failed_entries_are_not_copied_unless_asked_for(authed, model):
    ok, failed = _queue(authed, model, "2036-03-03"), _queue(authed, model, "2036-03-04")
    authed.patch(f"/api/queue/{failed['id']}", json={"status": "failed"})
    assert [c["source_id"] for c in authed.post("/api/calendar/copy", json={"date": MONDAY, "dry_run": True}).json()["created"]] == [ok["id"]]
    both = authed.post("/api/calendar/copy", json={"date": MONDAY, "statuses": ["queued", "failed"], "dry_run": True}).json()["created"]
    assert sorted(c["source_id"] for c in both) == sorted([ok["id"], failed["id"]])
    waiting_only = authed.post("/api/calendar/copy", json={"date": MONDAY, "statuses": ["failed"], "dry_run": True}).json()["created"]
    assert [c["source_id"] for c in waiting_only] == [failed["id"]]


def test_a_preview_adds_nothing(authed, model):
    _queue(authed, model, "2036-03-03")
    before = len(_all(authed))
    r = authed.post("/api/calendar/copy", json={"date": MONDAY, "weeks": 2, "dry_run": True}).json()
    assert r["dry_run"] is True and len(r["created"]) == 2 and r["activity_id"] is None and "id" not in r["created"][0]
    assert len(_all(authed)) == before


def test_an_empty_week_copies_nothing(authed, model):
    r = authed.post("/api/calendar/copy", json={"date": "2037-06-02"}).json()
    assert r["created"] == [] and r["activity_id"] is None


def test_the_copy_can_be_undone_but_a_print_that_began_stays(authed, model):
    _queue(authed, model, "2036-03-03")
    _queue(authed, model, "2036-03-04")
    r = authed.post("/api/calendar/copy", json={"date": MONDAY}).json()
    began = r["created"][0]["id"]
    authed.patch(f"/api/queue/{began}", json={"status": "printing"})
    undone = authed.post(f"/api/activity/{r['activity_id']}/undo")
    assert undone.status_code == 200 and undone.json()["restored"] == 1
    ids = {q["id"] for q in _all(authed)}
    assert began in ids and r["created"][1]["id"] not in ids
    assert authed.post(f"/api/activity/{r['activity_id']}/undo").status_code in (400, 409)


@pytest.mark.parametrize("payload", [{"date": "soon"}, {"date": MONDAY, "weeks": 0}, {"date": MONDAY, "weeks": 9}, {"date": MONDAY, "weeks": "2"},
                                     {"date": MONDAY, "weeks": True}, {"date": MONDAY, "statuses": "queued"}, {"date": MONDAY, "statuses": ["bogus"]},
                                     {"date": MONDAY, "statuses": []}])
def test_bad_requests_are_refused(authed, model, payload):
    _queue(authed, model, "2036-03-03")
    assert authed.post("/api/calendar/copy", json=payload).status_code == 400


def test_too_many_entries_are_refused(authed, model, monkeypatch):
    from app import calendar_plan
    monkeypatch.setattr(calendar_plan, "MAX_COPIED", 3)
    for day in ("2036-03-03", "2036-03-04"):
        _queue(authed, model, day)
    assert authed.post("/api/calendar/copy", json={"date": MONDAY, "weeks": 2}).status_code == 400
    assert authed.post("/api/calendar/copy", json={"date": MONDAY, "weeks": 1}).status_code == 200


def test_the_copies_show_on_the_calendar_and_viewers_cannot_copy(authed, model):
    from app.main import app
    _queue(authed, model, "2036-03-03")
    authed.post("/api/calendar/copy", json={"date": MONDAY})
    assert authed.get("/api/calendar", params={"month": "2036-03"}).json()["days"]["2036-03-10"]["planned"]
    authed.post("/api/users", json={"username": "cwviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "cwviewer", "password": PW})
        assert viewer.post("/api/calendar/copy", json={"date": MONDAY}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
