"""Print times learned from your own prints: a model's own history, and how far off estimates usually are."""
import uuid

import httpx
import pytest

from app import learned, printers as printing, printwatch
from test_printers import FakePrinter


def _stl():
    n = 30 + uuid.uuid4().int % 200
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


def _session():
    from sqlmodel import Session
    from app.db import engine
    return Session(engine)


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"le_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    yield m
    for q in authed.get("/api/queue").json():
        if q["model_id"] == m["id"]:
            authed.delete(f"/api/queue/{q['id']}")
    authed.delete(f"/api/library/models/{m['id']}")


@pytest.fixture(autouse=True)
def clean_history(authed):
    """Estimates are learned from the whole library's queue, so each test starts from none of it."""
    for q in authed.get("/api/queue").json():
        if q["status"] == "done":
            authed.delete(f"/api/queue/{q['id']}")
    yield


def _done_item(authed, model, estimated, actual, basis="manual"):
    """A finished queue entry whose estimate and real time are both known."""
    from app.models import QueueItem
    item = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": estimated}).json()
    with _session() as s:
        row = s.get(QueueItem, item["id"])
        row.status, row.actual_minutes, row.estimate_basis = "done", actual, basis
        s.add(row)
        s.commit()
    return item["id"]


def test_nothing_is_known_about_a_new_model(authed, model):
    assert authed.get(f"/api/estimates/{model['id']}").json() == {"minutes": None, "basis": None, "samples": 0}
    assert authed.get("/api/estimates/987654").status_code == 404


def test_a_figure_with_no_history_is_returned_as_it_is(authed, model):
    r = authed.get(f"/api/estimates/{model['id']}", params={"base": 100}).json()
    assert r == {"minutes": 100, "basis": "estimate", "samples": 0}


def test_a_models_own_measured_prints_win(authed, model):
    for minutes in (60, 70, 200):
        authed.post("/api/prints", json={"model_id": model["id"], "minutes": minutes, "deduct": False})
    r = authed.get(f"/api/estimates/{model['id']}", params={"base": 5}).json()
    assert r == {"minutes": 70, "basis": "history", "samples": 3}                            # the median, not the mean


def test_a_queue_entrys_own_estimate_is_not_history(authed, model):
    """Logging a finished queue entry copies its estimate, which must not later be mistaken for a measured time."""
    item = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 45}).json()
    authed.patch(f"/api/queue/{item['id']}", json={"status": "done"})
    log = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]
    assert log["minutes"] == 45 and not log["measured"]
    assert authed.get(f"/api/estimates/{model['id']}").json()["basis"] is None


def test_queueing_a_model_fills_in_the_learned_time_and_says_where_from(authed, model):
    authed.post("/api/prints", json={"model_id": model["id"], "minutes": 80, "deduct": False})
    learned_item = authed.post("/api/queue", json={"model_id": model["id"]}).json()
    assert learned_item["estimated_minutes"] == 80 and learned_item["estimate_basis"] == "history"
    typed = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 15}).json()
    assert typed["estimated_minutes"] == 15 and typed["estimate_basis"] == "manual"
    edited = authed.patch(f"/api/queue/{learned_item['id']}", json={"estimated_minutes": 90}).json()
    assert edited["estimate_basis"] == "manual"
    cleared = authed.patch(f"/api/queue/{learned_item['id']}", json={"estimated_minutes": None}).json()
    assert cleared["estimate_basis"] is None


def test_estimates_are_corrected_by_how_far_off_they_usually_are(authed, model):
    other = authed.post("/api/library/import", files={"file": (f"le_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    try:
        _done_item(authed, model, 60, 90)
        _done_item(authed, model, 100, 150)
        assert authed.get("/api/estimates/accuracy").json() == {"samples": 2, "factor": 1.5, "usable": False}
        assert authed.get(f"/api/estimates/{other['id']}", params={"base": 200}).json()["basis"] == "estimate"      # too few to trust
        _done_item(authed, model, 30, 45)
        acc = authed.get("/api/estimates/accuracy").json()
        assert acc == {"samples": 3, "factor": 1.5, "usable": True}
        r = authed.get(f"/api/estimates/{other['id']}", params={"base": 200}).json()
        assert r == {"minutes": 300, "basis": "adjusted", "samples": 3, "base": 200, "factor": 1.5}
    finally:
        authed.delete(f"/api/library/models/{other['id']}")


def test_corrections_that_came_from_history_do_not_count_as_evidence(authed, model):
    for _ in range(3):
        _done_item(authed, model, 60, 120, basis="history")
    assert authed.get("/api/estimates/accuracy").json() == {"samples": 0, "factor": None, "usable": False}


def test_one_wild_print_cannot_make_estimates_absurd(authed, model):
    for _ in range(3):
        _done_item(authed, model, 10, 1000)
    assert authed.get("/api/estimates/accuracy").json()["factor"] == learned.RATIO_RANGE[1]


def test_the_sliced_files_time_is_the_starting_figure(authed, model):
    gcode = b"G28\n; estimated printing time (normal mode) = 1h 30m 0s\n"
    authed.post("/api/print-files", data={"model_id": str(model["id"])}, files={"file": ("plate.gcode", gcode, "application/octet-stream")})
    assert authed.get(f"/api/estimates/{model['id']}").json() == {"minutes": 90, "basis": "estimate", "samples": 0}


def test_a_finished_print_remembers_how_long_it_really_took(authed, model, monkeypatch):
    fake = FakePrinter()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(fake), timeout=timeout))
    printwatch.reset()
    printer = authed.post("/api/printers", json={"name": "Learner", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    try:
        item = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 10, "printer_id": printer["id"]}).json()
        from app.models import PrinterJob
        with _session() as s:
            s.add(PrinterJob(printer_id=printer["id"], filename="benchy.gcode", model_id=model["id"], started=True))
            s.commit()
            fake.moonraker_state = "printing"
            printwatch.poll(s)
            fake.moonraker_state = "complete"
            printwatch.poll(s)
        row = next(q for q in authed.get("/api/queue").json() if q["id"] == item["id"])
        assert row["status"] == "done" and row["estimated_minutes"] == 10 and row["actual_minutes"] == 12.6
        log = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]
        assert log["minutes"] == 12.6 and log["measured"] is True
        assert authed.get(f"/api/estimates/{model['id']}").json() == {"minutes": 13, "basis": "history", "samples": 1}
    finally:
        authed.delete(f"/api/printers/{printer['id']}")


def test_logging_a_print_by_hand_with_a_time_counts_as_measured(authed, model):
    r = authed.post("/api/prints", json={"model_id": model["id"], "minutes": 33, "deduct": False}).json()
    assert r["measured"] is True
    no_time = authed.post("/api/prints", json={"model_id": model["id"], "deduct": False}).json()
    assert not no_time["measured"]
    edited = authed.patch(f"/api/prints/{no_time['id']}", json={"minutes": 44}).json()
    assert edited["measured"] is True
    assert authed.patch(f"/api/prints/{no_time['id']}", json={"minutes": None}).json()["measured"] is False


def test_old_rows_are_classified_when_the_column_is_first_added(tmp_path, monkeypatch):
    """A database from before this release has no measured column: it is added empty, then filled in once."""
    import sqlite3
    from sqlalchemy import create_engine
    from app import db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE printlog (id INTEGER PRIMARY KEY, model_id INTEGER, printed_at TEXT, grams REAL, deducted_g REAL, minutes REAL,"
                 " rating INTEGER, notes TEXT, source TEXT, queue_item_id INTEGER, created_at TEXT, filament_id INTEGER)")
    conn.executemany("INSERT INTO printlog (id, model_id, minutes, source) VALUES (?, 1, ?, ?)",
                     [(1, 50, "manual"), (2, 60, "printer"), (3, 70, "queue"), (4, None, "manual")])
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db._sync_columns()
    db._backfill()
    db._backfill()                                                                          # harmless a second time
    conn = sqlite3.connect(path)
    assert [r[0] for r in conn.execute("SELECT measured FROM printlog ORDER BY id")] == [1, 1, 0, 0]
    conn.close()


def test_deleting_a_queue_entry_lets_its_id_be_reused_without_hiding_the_next_print(authed, model):
    first = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 20}).json()
    authed.patch(f"/api/queue/{first['id']}", json={"status": "done"})
    authed.delete(f"/api/queue/{first['id']}")
    second = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 25}).json()
    authed.patch(f"/api/queue/{second['id']}", json={"status": "done"})
    logs = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"]
    assert sorted(l["minutes"] for l in logs) == [20, 25]


def test_the_estimates_need_a_login(authed):
    from starlette.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/api/estimates/accuracy").status_code == 401
