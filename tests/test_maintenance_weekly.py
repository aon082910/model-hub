"""Printer maintenance, the weekly summary, suggested settings and the slicer links."""
import uuid
from datetime import date, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from app import maintenance, signed_links, weekly

PW = "a long enough password"


def _stl():
    n = 30 + uuid.uuid4().int % 250
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


def _session():
    from sqlmodel import Session
    from app.db import engine
    return Session(engine)


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"mw_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


@pytest.fixture()
def printer(authed):
    p = authed.post("/api/printers", json={"name": "Maint", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    yield p
    for t in authed.get("/api/maintenance").json()["tasks"]:
        authed.delete(f"/api/maintenance/{t['id']}")
    for p2 in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p2['id']}")


def _log(c, model, printer_id, minutes, **extra):
    r = c.post("/api/prints", json={"model_id": model["id"], "minutes": minutes, "deduct": False, **extra})
    assert r.status_code == 200
    from app.models import PrintLog
    with _session() as s:
        row = s.get(PrintLog, r.json()["id"])
        row.printer_id = printer_id
        s.add(row)
        s.commit()
    return r.json()


# ---------- maintenance ----------

def test_tasks_need_a_printer_a_name_and_an_interval(authed, printer):
    ok = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Oil the rails", "every_hours": 200})
    assert ok.status_code == 200 and ok.json()["status"] == "ok" and ok.json()["printer"] == "Maint"
    for bad in ({"printer_id": 987654}, {"name": ""}, {"every_hours": None}, {"every_hours": 0}, {"every_hours": "x"}, {"every_days": 99999}, {"every_hours": True}):
        body = {"printer_id": printer["id"], "name": "x", "every_hours": 10, **bad}
        assert authed.post("/api/maintenance", json=body).status_code == 400, bad


def test_a_task_falls_due_after_the_printers_print_hours(authed, printer, model):
    task = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Replace nozzle", "every_hours": 10}).json()
    _log(authed, model, printer["id"], 300)                                           # 5 hours: half way
    row = next(t for t in authed.get("/api/maintenance").json()["tasks"] if t["id"] == task["id"])
    assert (row["hours_since"], row["hours_hold"] if "hours_hold" in row else row["hours_left"], row["status"]) == (5.0, 5.0, "ok")
    _log(authed, model, printer["id"], 240)                                           # 9 hours: soon
    assert next(t for t in authed.get("/api/maintenance").json()["tasks"] if t["id"] == task["id"])["status"] == "soon"
    _log(authed, model, printer["id"], 90, outcome="failed")                          # 10.5 hours, a failed print's time counts: due
    due = next(t for t in authed.get("/api/maintenance").json()["tasks"] if t["id"] == task["id"])
    assert due["status"] == "due" and due["hours_left"] == -0.5


def test_prints_before_the_task_was_added_do_not_count(authed, printer, model):
    _log(authed, model, printer["id"], 6000)                                          # 100 hours already
    task = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Clean", "every_hours": 50}).json()
    assert task["hours_since"] == 0 and task["status"] == "ok"


def test_a_task_falls_due_after_days_and_marking_it_done_starts_again(authed, printer):
    task = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Clean the fans", "every_days": 30}).json()
    from app.models import MaintenanceTask
    with _session() as s:
        row = s.get(MaintenanceTask, task["id"])
        row.last_done_at = datetime.utcnow() - timedelta(days=45)
        s.add(row)
        s.commit()
    due = next(t for t in authed.get("/api/maintenance").json()["tasks"] if t["id"] == task["id"])
    assert due["status"] == "due" and due["days_since"] == 45 and due["days_left"] == -15
    done = authed.post(f"/api/maintenance/{task['id']}/done").json()
    assert done["status"] == "ok" and done["days_since"] == 0
    assert any("Maintenance done" in e["summary"] for e in authed.get("/api/activity", params={"action": "maintenance"}).json()["items"])


def test_the_most_urgent_task_is_listed_first_and_presets_are_offered(authed, printer, model):
    a = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Slow", "every_hours": 1000}).json()
    b = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Quick", "every_hours": 2}).json()
    _log(authed, model, printer["id"], 90)
    data = authed.get("/api/maintenance").json()
    assert [t["id"] for t in data["tasks"]] == [b["id"], a["id"]] and data["presets"]


def test_a_task_can_be_edited_and_deleted_and_goes_with_its_printer(authed, printer):
    task = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Old name", "every_hours": 10}).json()
    edited = authed.patch(f"/api/maintenance/{task['id']}", json={"name": "New name", "every_hours": None, "every_days": 7, "note": "see the manual"}).json()
    assert edited["name"] == "New name" and edited["every_hours"] is None and edited["every_days"] == 7 and edited["note"] == "see the manual"
    assert authed.patch(f"/api/maintenance/{task['id']}", json={"every_days": None}).status_code == 400            # an interval is needed
    other = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Goes with printer", "every_hours": 10}).json()
    authed.delete(f"/api/printers/{printer['id']}")
    assert authed.get("/api/maintenance").json()["tasks"] == []
    assert authed.delete(f"/api/maintenance/{other['id']}").status_code == 404


def test_a_due_task_is_announced_once(authed, printer, monkeypatch):
    sent = []
    monkeypatch.setattr(maintenance, "notify_event", lambda session, event, title, message: sent.append((event, message)))
    task = authed.post("/api/maintenance", json={"printer_id": printer["id"], "name": "Overdue", "every_days": 1}).json()
    from app.models import MaintenanceTask
    with _session() as s:
        row = s.get(MaintenanceTask, task["id"])
        row.last_done_at = datetime.utcnow() - timedelta(days=5)
        s.add(row)
        s.commit()
        assert maintenance.check_and_notify(s) == ["Maint: Overdue"] and maintenance.check_and_notify(s) == []
    assert len(sent) == 1 and sent[0][0] == "maintenance_due"
    authed.post(f"/api/maintenance/{task['id']}/done")
    with _session() as s:
        assert maintenance.check_and_notify(s) == []


def test_viewers_can_look_but_not_change(authed, printer):
    from app.main import app
    authed.post("/api/users", json={"username": "mtviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "mtviewer", "password": PW})
        assert viewer.get("/api/maintenance").status_code == 200
        assert viewer.post("/api/maintenance", json={"printer_id": printer["id"], "name": "x", "every_days": 3}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


# ---------- the weekly summary ----------

@pytest.fixture()
def sent(monkeypatch, authed):
    messages = []
    monkeypatch.setattr(weekly, "notify_event", lambda session, event, title, message: messages.append((event, title, message)) or True)
    authed.put("/api/settings", json={"weekly_summary": "", "weekly_summary_last": ""})
    yield messages
    authed.put("/api/settings", json={"weekly_summary": "", "weekly_summary_last": ""})


def test_nothing_is_sent_unless_switched_on(authed, sent):
    with _session() as s:
        assert weekly.scheduled(s) is False
    assert sent == []


def test_switching_it_on_starts_the_first_week_from_that_day(authed, sent):
    authed.put("/api/settings", json={"weekly_summary": "true"})
    with _session() as s:
        assert weekly.scheduled(s) is False                                      # sets the start date, sends nothing
    assert sent == [] and authed.get("/api/settings").json()["weekly_summary_last"] == date.today().isoformat()


def test_it_is_sent_when_seven_days_have_passed_and_not_again_until_the_next_week(authed, sent):
    authed.put("/api/settings", json={"weekly_summary": "true", "weekly_summary_last": (date.today() - timedelta(days=7)).isoformat()})
    with _session() as s:
        assert weekly.scheduled(s) is True and weekly.scheduled(s) is False
    assert len(sent) == 1 and sent[0][0] == "weekly_summary" and sent[0][2].startswith("Last week:")
    authed.put("/api/settings", json={"weekly_summary_last": (date.today() - timedelta(days=6)).isoformat()})
    with _session() as s:
        assert weekly.scheduled(s) is False
    authed.put("/api/settings", json={"weekly_summary_last": "garbage"})
    with _session() as s:
        assert weekly.scheduled(s) is True                                       # a damaged date means "send"


def test_the_summary_counts_the_weeks_prints_failures_and_what_is_waiting(authed, model, sent):
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "wk", "spool_weight_g": 1000, "remaining_g": 900, "cost": 20}).json()
    try:
        authed.post("/api/prints", json={"model_id": model["id"], "minutes": 120, "grams": 100, "filament_id": spool["id"], "deduct": False})
        authed.post("/api/prints", json={"model_id": model["id"], "outcome": "failed", "failure_reason": "clog", "deduct": False})
        old = authed.post("/api/prints", json={"model_id": model["id"], "printed_at": (date.today() - timedelta(days=30)).isoformat(), "minutes": 999, "deduct": False}).json()
        q = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 90, "planned_date": date.today().isoformat()}).json()
        with _session() as s:
            text = weekly.build(s)
        assert "Last week:" in text and "h of printing" in text and "g of filament" in text and "$" in text      # other tests' prints may be in the week too
        assert "failed, most often: Clogged nozzle or under-extrusion." in text
        assert "waiting in the queue" in text and "planned for the coming week" in text
        assert "999" not in text and old["id"]
        authed.delete(f"/api/queue/{q['id']}")
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_a_test_summary_can_be_sent_now_by_the_administrator_only(authed, sent):
    from app.main import app
    r = authed.post("/api/settings/weekly-test")
    assert r.status_code == 200 and r.json()["message"].startswith("Last week:") and len(sent) == 1
    authed.post("/api/users", json={"username": "wkmember", "password": PW, "role": "member"})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "wkmember", "password": PW})
        assert member.post("/api/settings/weekly-test").status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


def test_the_scheduler_has_the_new_jobs_and_events(authed):
    from app import scheduler
    from app.notify import EVENTS
    assert {"maintenance", "weekly"} <= set(scheduler.INTERVALS) and {"maintenance", "weekly"} <= set(scheduler._jobs())
    assert {"maintenance_due", "weekly_summary"} <= set(EVENTS)


# ---------- suggested settings ----------

def _spool(c, material, color="sg"):
    return c.post("/api/filament", json={"material": material, "color": color, "spool_weight_g": 1000, "remaining_g": 900}).json()


def test_nothing_is_suggested_before_anything_was_printed(authed, model):
    r = authed.get(f"/api/library/models/{model['id']}/suggested-settings").json()
    assert r["suggested"] == {"based_on": 0, "avoid": []} and r["current"] == {}
    assert authed.get("/api/library/models/987654/suggested-settings").status_code == 404


def test_the_material_that_printed_best_is_suggested_and_a_failing_one_is_to_avoid(authed, model):
    pla, petg = _spool(authed, "PLA"), _spool(authed, "PETG")
    try:
        for rating in (5, 4):
            authed.post("/api/prints", json={"model_id": model["id"], "filament_id": pla["id"], "rating": rating, "deduct": False})
        authed.post("/api/prints", json={"model_id": model["id"], "filament_id": petg["id"], "rating": 2, "deduct": False})
        for _ in range(2):
            authed.post("/api/prints", json={"model_id": model["id"], "filament_id": petg["id"], "outcome": "failed", "failure_reason": "warping", "deduct": False})
        s = authed.get(f"/api/library/models/{model['id']}/suggested-settings").json()["suggested"]
        assert s["material"] == "PLA" and s["avoid"] == ["PETG"] and s["based_on"] == 3
    finally:
        for sp in (pla, petg):
            authed.delete(f"/api/filament/{sp['id']}")


def test_a_material_that_keeps_failing_is_never_the_suggestion(authed, model):
    petg = _spool(authed, "PETG")
    try:
        authed.post("/api/prints", json={"model_id": model["id"], "filament_id": petg["id"], "rating": 5, "deduct": False})
        for _ in range(3):
            authed.post("/api/prints", json={"model_id": model["id"], "filament_id": petg["id"], "outcome": "failed", "deduct": False})
        s = authed.get(f"/api/library/models/{model['id']}/suggested-settings").json()["suggested"]
        assert "material" not in s and s["avoid"] == ["PETG"]
    finally:
        authed.delete(f"/api/filament/{petg['id']}")


def test_the_layer_height_comes_from_the_sliced_file_that_printed_well(authed, model):
    gcode = b"G28\n; layer_height = 0.16\n; filament_type = PETG\n; estimated printing time (normal mode) = 40m\n"
    kept = authed.post("/api/print-files", data={"model_id": str(model["id"])}, files={"file": ("plate.gcode", gcode, "application/octet-stream")}).json()
    from app.models import PrinterJob
    with _session() as s:
        s.add(PrinterJob(printer_id=1, filename="plate.gcode", model_id=model["id"], print_file_id=kept["id"], started=True, outcome="done"))
        s.commit()
    s = authed.get(f"/api/library/models/{model['id']}/suggested-settings").json()["suggested"]
    assert s["layer_height"] and "0.16" in str(s["layer_height"]) and s["from_file"] == "plate.gcode" and s.get("material") == "PETG"


# ---------- opening in a slicer ----------

def test_a_link_works_for_that_model_only_and_only_for_a_while(authed, model):
    r = authed.get(f"/api/slicer-link/{model['id']}").json()
    assert r["path"].startswith(f"/dl/{model['id']}/") and r["expires_in"] == 900 and set(r["schemes"]) == {"prusaslicer", "orcaslicer", "bambustudio"}
    from app.main import app
    anon = TestClient(app)
    got = anon.get(r["path"])
    assert got.status_code == 200 and got.content.startswith(b"solid") and got.headers["cache-control"] == "no-store"
    parts = r["path"].split("/")
    other = authed.post("/api/library/import", files={"file": (f"mw_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    try:
        assert anon.get(f"/dl/{other['id']}/{parts[3]}/{parts[4]}/x.stl").status_code == 404           # the same signature for another model
        assert anon.get(f"/dl/{model['id']}/{int(parts[3]) + 100}/{parts[4]}/x.stl").status_code == 404   # a longer life
        assert anon.get(f"/dl/{model['id']}/{parts[3]}/{'0' * 40}/x.stl").status_code == 404
        assert anon.get(f"/dl/{model['id']}/abc/{parts[4]}/x.stl").status_code == 404
    finally:
        authed.delete(f"/api/library/models/{other['id']}")


def test_an_expired_link_stops_working():
    expires, signature = signed_links.make(7, now=1000)
    assert signed_links.valid(7, expires, signature, now=1000 + 899) is True
    assert signed_links.valid(7, expires, signature, now=1000 + 901) is False
    assert signed_links.valid(8, expires, signature, now=1000) is False and signed_links.valid(7, "x", signature) is False


def test_only_models_a_slicer_can_open_get_a_link_and_a_login_is_needed(authed, model):
    from app.main import app
    other = authed.post("/api/library/import", files={"file": ("readme_" + uuid.uuid4().hex[:5] + ".stl", _stl(), "application/octet-stream")}).json()
    try:
        assert authed.get(f"/api/slicer-link/{other['id']}").status_code == 200
        assert authed.get("/api/slicer-link/987654").status_code == 404
        assert TestClient(app).get(f"/api/slicer-link/{model['id']}").status_code == 401
    finally:
        authed.delete(f"/api/library/models/{other['id']}")
