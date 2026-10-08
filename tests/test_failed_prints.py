"""Failed prints: kept with a reason, counted on their own, never counted as the model having been printed."""
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from app import printers as printing, printwatch
from test_printers import FakePrinter

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
    m = authed.post("/api/library/import", files={"file": (f"fp_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    yield m
    for q in authed.get("/api/queue").json():
        if q["model_id"] == m["id"]:
            authed.delete(f"/api/queue/{q['id']}")
    authed.delete(f"/api/library/models/{m['id']}")


def _log(c, model, **fields):
    r = c.post("/api/prints", json={"model_id": model["id"], "deduct": False, **fields})
    assert r.status_code == 200, r.text
    return r.json()


def _model(c, model):
    return c.get(f"/api/library/models/{model['id']}").json()


def test_the_reasons_are_listed(authed):
    reasons = authed.get("/api/prints/reasons").json()
    assert {"key": "bed_adhesion", "label": "Would not stick to the bed"} in reasons and any(r["key"] == "other" for r in reasons)


def test_a_failed_print_is_logged_with_its_reason(authed, model):
    log = _log(authed, model, outcome="failed", failure_reason="warping", minutes=95, notes="corner lifted")
    assert log["outcome"] == "failed" and log["failure_reason"] == "warping" and log["failure_label"] == "Warped or curled"
    assert log["measured"] is False                                              # part of a print is not how long it takes
    assert authed.get("/api/prints", params={"model_id": model["id"], "failed": True}).json()["total"] == 1
    assert authed.get("/api/prints", params={"model_id": model["id"], "failed": False}).json()["total"] == 0


def test_a_failure_never_makes_a_model_count_as_printed(authed, model):
    _log(authed, model, outcome="failed", failure_reason="spaghetti")
    m = _model(authed, model)
    assert m["print_count"] == 0 and m["last_printed_at"] is None
    never = [x["id"] for x in authed.get("/api/library/models", params={"printed": "false", "limit": 5000}).json()]
    printed = [x["id"] for x in authed.get("/api/library/models", params={"printed": "true", "limit": 5000}).json()]
    assert model["id"] in never and model["id"] not in printed
    _log(authed, model, minutes=40)
    assert _model(authed, model)["print_count"] == 1
    printed = [x["id"] for x in authed.get("/api/library/models", params={"printed": "true", "limit": 5000}).json()]
    assert model["id"] in printed


def test_failures_do_not_feed_the_learned_times(authed, model):
    _log(authed, model, outcome="failed", minutes=999)
    assert authed.get(f"/api/estimates/{model['id']}").json()["basis"] is None
    _log(authed, model, minutes=45)
    assert authed.get(f"/api/estimates/{model['id']}").json() == {"minutes": 45, "basis": "history", "samples": 1}


def test_bad_outcomes_and_reasons_are_refused(authed, model):
    assert authed.post("/api/prints", json={"model_id": model["id"], "outcome": "exploded"}).status_code == 400
    assert authed.post("/api/prints", json={"model_id": model["id"], "outcome": "failed", "failure_reason": "gremlins"}).status_code == 400
    ok = _log(authed, model)
    assert authed.patch(f"/api/prints/{ok['id']}", json={"failure_reason": "warping"}).status_code == 400    # only a failed print has one
    assert authed.patch(f"/api/prints/{ok['id']}", json={"outcome": "nope"}).status_code == 400
    assert _log(authed, model, failure_reason="warping")["failure_reason"] is None                              # ignored when it did not fail


def test_a_print_can_be_marked_failed_afterwards_and_back(authed, model):
    log = _log(authed, model, minutes=30)
    assert log["measured"] is True and _model(authed, model)["print_count"] == 1
    failed = authed.patch(f"/api/prints/{log['id']}", json={"outcome": "failed", "failure_reason": "clog"}).json()
    assert failed["outcome"] == "failed" and failed["failure_reason"] == "clog" and failed["measured"] is False
    assert _model(authed, model)["print_count"] == 0
    back = authed.patch(f"/api/prints/{log['id']}", json={"outcome": "done"}).json()
    assert back["outcome"] is None and back["failure_reason"] is None and back["measured"] is True
    assert _model(authed, model)["print_count"] == 1


def test_a_queue_entry_marked_failed_logs_a_failure_once_and_a_later_success_is_separate(authed, model):
    item = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 50}).json()
    authed.patch(f"/api/queue/{item['id']}", json={"status": "failed", "failure_reason": "bed_adhesion"})
    authed.patch(f"/api/queue/{item['id']}", json={"status": "queued"})
    authed.patch(f"/api/queue/{item['id']}", json={"status": "failed"})                     # failing again does not duplicate it
    logs = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"]
    assert [l["outcome"] for l in logs] == ["failed"] and logs[0]["failure_reason"] == "bed_adhesion"
    authed.patch(f"/api/queue/{item['id']}", json={"status": "queued"})
    authed.patch(f"/api/queue/{item['id']}", json={"status": "done"})                      # the retry worked
    logs = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"]
    assert sorted((l["outcome"] or "done") for l in logs) == ["done", "failed"]
    assert _model(authed, model)["print_count"] == 1


def test_a_failed_queue_entry_takes_nothing_off_a_spool(authed, model):
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "fail-test", "spool_weight_g": 1000, "remaining_g": 500}).json()
    try:
        item = authed.post("/api/queue", json={"model_id": model["id"], "filament_id": spool["id"], "estimated_grams": 80}).json()
        authed.patch(f"/api/queue/{item['id']}", json={"status": "failed"})
        assert next(f for f in authed.get("/api/filament").json() if f["id"] == spool["id"])["remaining_g"] == 500
        log = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]
        edited = authed.patch(f"/api/prints/{log['id']}", json={"grams": 20}).json()                       # say what was wasted: it comes off the spool
        assert edited["grams"] == 20
        assert next(f for f in authed.get("/api/filament").json() if f["id"] == spool["id"])["remaining_g"] == 480
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_print_again_copies_the_last_good_print_not_a_failure(authed, model):
    _log(authed, model, minutes=33)
    _log(authed, model, minutes=7, outcome="failed")
    again = authed.post(f"/api/queue/again/{model['id']}").json()
    assert again["estimated_minutes"] == 33


def test_stats_count_failures_on_their_own_with_reasons_and_waste(authed, model):
    spool = authed.post("/api/filament", json={"material": "PETG", "color": "stat-fail", "spool_weight_g": 1000, "remaining_g": 900, "cost": 20}).json()
    try:
        before = authed.get("/api/stats").json()
        _log(authed, model, minutes=60, grams=50, filament_id=spool["id"])
        _log(authed, model, outcome="failed", failure_reason="clog", grams=100, filament_id=spool["id"])
        _log(authed, model, outcome="failed", failure_reason="clog")
        _log(authed, model, outcome="failed")
        after = authed.get("/api/stats").json()
        assert after["totals"]["prints"] == before["totals"]["prints"] + 1
        f, f0 = after["failures"], before["failures"]
        assert f["total"] == f0["total"] + 3 and f["grams"] == round(f0["grams"] + 100, 1) and f["cost"] == round(f0["cost"] + 2.0, 2)
        reasons = {r["reason"]: r for r in f["by_reason"]}
        assert reasons["clog"]["label"] == "Clogged nozzle or under-extrusion" and reasons["clog"]["count"] >= 2
        assert reasons["unsaid"]["label"] == "No reason given" and f["rate"] is not None
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_the_calendar_marks_failed_prints(authed, model):
    _log(authed, model, printed_at="2033-02-03", outcome="failed", failure_reason="power")
    day = authed.get("/api/calendar", params={"month": "2033-02"}).json()["days"]["2033-02-03"]
    assert day["printed"][0]["failed"] is True and day["printed"][0]["reason"] == "power"


def test_a_stopped_print_is_logged_as_a_failure_and_the_entry_is_marked_failed(authed, model, monkeypatch):
    fake = FakePrinter()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(fake), timeout=timeout))
    printwatch.reset()
    printer = authed.post("/api/printers", json={"name": "Failer", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    try:
        item = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 60, "printer_id": printer["id"]}).json()
        from app.models import PrinterJob
        with _session() as s:
            s.add(PrinterJob(printer_id=printer["id"], filename="benchy.gcode", model_id=model["id"], started=True))
            s.commit()
            fake.moonraker_state = "printing"
            printwatch.poll(s)
            fake.moonraker_state = "error"
            assert printwatch.poll(s) == [("Failer", "stopped")]
        row = next(q for q in authed.get("/api/queue").json() if q["id"] == item["id"])
        assert row["status"] == "failed"
        logs = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"]
        assert len(logs) == 1 and logs[0]["outcome"] == "failed" and logs[0]["minutes"] == 12.6 and logs[0]["measured"] is False
        why = authed.patch(f"/api/prints/{logs[0]['id']}", json={"failure_reason": "layer_shift"}).json()
        assert why["failure_reason"] == "layer_shift"
        assert _model(authed, model)["print_count"] == 0
    finally:
        authed.delete(f"/api/printers/{printer['id']}")


def test_old_log_entries_without_an_outcome_count_as_prints(authed, model):
    from app.models import PrintLog
    with _session() as s:
        row = PrintLog(model_id=model["id"], minutes=20, source="manual")
        s.add(row)
        s.commit()
    assert _model(authed, model)["print_count"] == 1


def test_viewers_cannot_log_failures(authed, model):
    from app.main import app
    authed.post("/api/users", json={"username": "failviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "failviewer", "password": PW})
        assert viewer.get("/api/prints/reasons").status_code == 200
        assert viewer.post("/api/prints", json={"model_id": model["id"], "outcome": "failed"}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
