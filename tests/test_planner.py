"""A calendar of planned prints: month view, planning into free days, undo and the .ics file."""
import uuid
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

PW = "a long enough password"


def _stl():
    n = 30 + uuid.uuid4().int % 250
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"pl_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


@pytest.fixture(autouse=True)
def empty_queue(authed):
    def wipe():
        for q in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{q['id']}")
        for p in authed.get("/api/printers").json()["printers"]:
            authed.delete(f"/api/printers/{p['id']}")
        authed.put("/api/settings", json={"calendar_hours_per_day": ""})
    wipe()
    yield
    wipe()


def _queue(c, model, minutes=None, **extra):
    r = c.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": minutes, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _month(c, month):
    r = c.get("/api/calendar", params={"month": month})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_queue_entry_can_be_planned_for_a_day_and_the_date_is_checked(authed, model):
    item = _queue(authed, model, 60, planned_date="2031-03-05")
    assert item["planned_date"] == "2031-03-05"
    assert authed.patch(f"/api/queue/{item['id']}", json={"planned_date": "2031-03-09"}).json()["planned_date"] == "2031-03-09"
    for bad in ("tomorrow", "2031-3-5", "2031-02-30", "2031-13-01", 20310305):
        assert authed.patch(f"/api/queue/{item['id']}", json={"planned_date": bad}).status_code == 400
    assert authed.post("/api/queue", json={"model_id": model["id"], "planned_date": "soon"}).status_code == 400
    assert authed.patch(f"/api/queue/{item['id']}", json={"planned_date": None}).json()["planned_date"] is None


def test_the_month_shows_planned_prints_on_their_days_with_the_time_they_need(authed, model):
    a = _queue(authed, model, 90, planned_date="2031-03-05")
    _queue(authed, model, 30, planned_date="2031-03-05")
    _queue(authed, model, 45, planned_date="2031-04-01")                     # another month
    done = _queue(authed, model, 20, planned_date="2031-03-06")
    authed.patch(f"/api/queue/{done['id']}", json={"status": "done"})        # finished: no longer "planned"
    view = _month(authed, "2031-03")
    day = view["days"]["2031-03-05"]
    assert [p["id"] for p in day["planned"]][0] == a["id"] and day["minutes"] == 120 and day["overbooked"] is False
    assert day["planned"][0]["filename"] == model["filename"]
    assert "2031-04-01" not in view["days"]
    assert not view["days"].get("2031-03-06", {}).get("planned")
    assert (view["first"], view["last"]) == ("2031-03-01", "2031-03-31")


def test_a_day_with_more_than_the_printers_can_do_is_marked(authed, model):
    _queue(authed, model, 90, planned_date="2031-05-02")
    authed.put("/api/settings", json={"calendar_hours_per_day": "1"})
    view = _month(authed, "2031-05")
    assert view["capacity_minutes"] == 60 and view["days"]["2031-05-02"]["overbooked"] is True
    authed.post("/api/printers", json={"name": "A", "kind": "moonraker", "url": "http://klipper.local:7125"})
    authed.post("/api/printers", json={"name": "B", "kind": "moonraker", "url": "http://klipper.local:7126"})
    view = _month(authed, "2031-05")
    assert view["capacity_minutes"] == 120 and view["days"]["2031-05-02"]["overbooked"] is False        # two printers share the day
    authed.put("/api/settings", json={"calendar_hours_per_day": "nonsense"})
    assert _month(authed, "2031-05")["hours_per_day"] == 12.0


def test_an_entry_without_an_estimate_still_takes_room(authed, model):
    _queue(authed, model, None, planned_date="2031-06-01")
    view = _month(authed, "2031-06")
    assert view["days"]["2031-06-01"]["minutes"] == 60 and view["days"]["2031-06-01"]["planned"][0]["minutes"] is None


def test_prints_already_made_appear_on_the_day_they_were_logged(authed, model):
    authed.post("/api/prints", json={"model_id": model["id"], "printed_at": "2031-07-04", "minutes": 75, "deduct": False})
    day = _month(authed, "2031-07")["days"]["2031-07-04"]
    assert day["printed"][0]["minutes"] == 75 and day["printed"][0]["filename"] == model["filename"] and day["printed"][0]["has_photo"] is False


def test_waiting_entries_without_a_day_are_listed(authed, model):
    a = _queue(authed, model, 10)
    _queue(authed, model, 10, planned_date="2031-08-01")
    view = _month(authed, "2031-08")
    assert [u["id"] for u in view["unplanned"]] == [a["id"]] and view["unplanned_total"] == 1


def test_a_bad_month_is_refused(authed):
    for bad in ("2031-13", "2031", "march", "2031-3"):
        assert authed.get("/api/calendar", params={"month": bad}).status_code == 400
    assert authed.get("/api/calendar").status_code == 200                       # no month: this one


def test_planning_fills_days_in_queue_order_without_overfilling(authed, model):
    authed.put("/api/settings", json={"calendar_hours_per_day": "3"})           # 180 minutes a day
    first = _queue(authed, model, 120)
    second = _queue(authed, model, 90)                                          # does not fit with the first
    third = _queue(authed, model, 50)                                           # fits beside the first (170)
    start = "2031-09-01"
    r = authed.post("/api/calendar/plan", json={"start": start, "dry_run": True}).json()
    assert r["dry_run"] is True and r["activity_id"] is None
    assert authed.get("/api/queue").json()[0]["planned_date"] is None                                  # a preview changes nothing
    r = authed.post("/api/calendar/plan", json={"start": start}).json()
    days = {a["id"]: a["planned_date"] for a in r["assigned"]}
    assert days == {first["id"]: "2031-09-01", second["id"]: "2031-09-02", third["id"]: "2031-09-01"} and r["unplaced"] == 0
    assert {q["id"]: q["planned_date"] for q in authed.get("/api/queue").json()} == days


def test_planning_respects_what_is_already_planned_and_leaves_planned_entries_alone(authed, model):
    authed.put("/api/settings", json={"calendar_hours_per_day": "2"})
    fixed = _queue(authed, model, 100, planned_date="2031-10-01")
    waiting = _queue(authed, model, 60)
    authed.post("/api/calendar/plan", json={"start": "2031-10-01"})
    rows = {q["id"]: q["planned_date"] for q in authed.get("/api/queue").json()}
    assert rows[fixed["id"]] == "2031-10-01" and rows[waiting["id"]] == "2031-10-02"


def test_an_entry_longer_than_a_day_gets_a_day_to_itself(authed, model):
    authed.put("/api/settings", json={"calendar_hours_per_day": "1"})
    long_one = _queue(authed, model, 500)
    small = _queue(authed, model, 20)
    authed.post("/api/calendar/plan", json={"start": "2031-11-01"})
    rows = {q["id"]: q["planned_date"] for q in authed.get("/api/queue").json()}
    assert rows[long_one["id"]] == "2031-11-01" and rows[small["id"]] == "2031-11-02"


def test_planning_with_nothing_waiting_is_harmless(authed):
    r = authed.post("/api/calendar/plan", json={}).json()
    assert r["assigned"] == [] and r["unplaced"] == 0 and r["activity_id"] is None
    assert authed.post("/api/calendar/plan", json={"start": "soon"}).status_code == 400


def test_a_planning_run_can_be_undone_but_only_where_nothing_changed_since(authed, model):
    one, two = _queue(authed, model, 30), _queue(authed, model, 30)
    r = authed.post("/api/calendar/plan", json={"start": "2032-01-10"}).json()
    authed.patch(f"/api/queue/{two['id']}", json={"planned_date": "2032-02-20"})                      # moved by hand afterwards
    undone = authed.post(f"/api/activity/{r['activity_id']}/undo")
    assert undone.status_code == 200, undone.text
    rows = {q["id"]: q["planned_date"] for q in authed.get("/api/queue").json()}
    assert rows[one["id"]] is None and rows[two["id"]] == "2032-02-20"
    assert authed.post(f"/api/activity/{r['activity_id']}/undo").status_code in (400, 409)             # only once


def test_the_ics_file_lists_planned_prints_as_all_day_events(authed, model):
    one = _queue(authed, model, 90, planned_date="2032-03-04", notes="ignored")
    finished = _queue(authed, model, 10, planned_date="2032-03-05")
    authed.patch(f"/api/queue/{finished['id']}", json={"status": "done"})
    _queue(authed, model, 10)                                                                   # no day: not in the file
    r = authed.get("/api/calendar/export.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar") and "attachment" in r.headers["content-disposition"]
    text = r.text
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert text.count("BEGIN:VEVENT") == 1 and f"UID:queue-{one['id']}@modelhub" in text
    assert "DTSTART;VALUE=DATE:20320304" in text and "DTEND;VALUE=DATE:20320305" in text and "about 90 min" in text


def test_text_in_the_ics_file_cannot_break_out_of_its_line(authed):
    from app import calendar_plan
    assert calendar_plan._ics_text("a,b;c\\d\nINJECT:1\r") == "a\\,b\\;c\\\\d\\nINJECT:1 "
    folded = calendar_plan._fold("SUMMARY:" + "é" * 100)
    assert all(len(line.encode()) <= 75 for line in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == "SUMMARY:" + "é" * 100


def test_a_viewer_can_look_at_the_calendar_but_not_plan(authed, model):
    from app.main import app
    authed.post("/api/users", json={"username": "calviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "calviewer", "password": PW})
        assert viewer.get("/api/calendar").status_code == 200
        assert viewer.get("/api/calendar/export.ics").status_code == 200
        assert viewer.post("/api/calendar/plan", json={}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert TestClient(app).get("/api/calendar").status_code == 401


def test_the_planner_starts_today_by_default(authed, model):
    authed.put("/api/settings", json={"calendar_hours_per_day": "12"})
    item = _queue(authed, model, 30)
    r = authed.post("/api/calendar/plan", json={}).json()
    assert r["assigned"][0]["planned_date"] == date.today().isoformat() and r["assigned"][0]["id"] == item["id"]
