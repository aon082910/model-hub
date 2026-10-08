"""What has gone wrong with a model before: hints on its page, the queue and the library filter."""
import uuid

import pytest
from starlette.testclient import TestClient

PW = "a long enough password"


def _stl():
    n = 30 + uuid.uuid4().int % 250
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"fh_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


def _log(c, model, **fields):
    r = c.post("/api/prints", json={"model_id": model["id"], "deduct": False, **fields})
    assert r.status_code == 200, r.text
    return r.json()


def _hint(c, model):
    return c.get("/api/prints/hints", params={"model_id": model["id"]}).json()["hints"].get(str(model["id"]))


def test_a_model_that_never_failed_has_no_hint(authed, model):
    _log(authed, model, minutes=30)
    assert _hint(authed, model) is None


def test_the_hint_counts_attempts_names_the_usual_reason_and_gives_a_tip(authed, model):
    _log(authed, model, minutes=30)
    _log(authed, model, minutes=30)
    _log(authed, model, outcome="failed", failure_reason="warping")
    _log(authed, model, outcome="failed", failure_reason="warping")
    _log(authed, model, outcome="failed", failure_reason="clog")
    h = _hint(authed, model)
    assert (h["prints"], h["failures"], h["attempts"], h["rate"]) == (2, 3, 5, 60)
    assert h["top_reason"] == "warping" and h["top_label"] == "Warped or curled" and "brim" in h["tip"]
    assert h["last_failure"]


def test_failures_without_a_reason_have_no_top_reason_and_ties_pick_the_first_alphabetically(authed, model):
    _log(authed, model, outcome="failed")
    h = _hint(authed, model)
    assert h["top_reason"] is None and h["tip"] is None and h["failures"] == 1 and h["prints"] == 0
    _log(authed, model, outcome="failed", failure_reason="warping")
    _log(authed, model, outcome="failed", failure_reason="clog")
    assert _hint(authed, model)["top_reason"] == "clog"


def test_the_material_failures_happened_on_is_listed(authed, model):
    petg = authed.post("/api/filament", json={"material": "PETG", "color": "hint-a", "spool_weight_g": 1000, "remaining_g": 900}).json()
    pla = authed.post("/api/filament", json={"material": "PLA", "color": "hint-b", "spool_weight_g": 1000, "remaining_g": 900}).json()
    try:
        _log(authed, model, outcome="failed", failure_reason="warping", filament_id=petg["id"])
        _log(authed, model, outcome="failed", failure_reason="warping", filament_id=petg["id"])
        _log(authed, model, outcome="failed", failure_reason="clog", filament_id=pla["id"])
        _log(authed, model, outcome="failed", failure_reason="clog")
        assert _hint(authed, model)["materials"] == [{"material": "PETG", "failures": 2}, {"material": "PLA", "failures": 1}]
    finally:
        for s in (petg, pla):
            authed.delete(f"/api/filament/{s['id']}")


def test_without_a_model_every_model_that_failed_is_listed(authed, model):
    other = authed.post("/api/library/import", files={"file": (f"fh_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    try:
        _log(authed, model, outcome="failed", failure_reason="spaghetti")
        _log(authed, other, minutes=10)
        hints = authed.get("/api/prints/hints").json()["hints"]
        assert str(model["id"]) in hints and str(other["id"]) not in hints
    finally:
        authed.delete(f"/api/library/models/{other['id']}")


def test_the_library_can_be_filtered_to_models_that_failed_before(authed, model):
    clean = authed.post("/api/library/import", files={"file": (f"fh_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    try:
        _log(authed, model, outcome="failed", failure_reason="clog")
        _log(authed, clean, minutes=5)
        ids = [m["id"] for m in authed.get("/api/library/models", params={"failed_before": "true", "limit": 5000}).json()]
        assert model["id"] in ids and clean["id"] not in ids
        everything = [m["id"] for m in authed.get("/api/library/models", params={"limit": 5000}).json()]
        assert {model["id"], clean["id"]} <= set(everything)
        saved = authed.post("/api/saved-searches", json={"name": f"failed {uuid.uuid4().hex[:4]}", "params": {"failed_before": True}})
        assert saved.status_code == 200, saved.text
        authed.delete(f"/api/saved-searches/{saved.json()['id']}")
    finally:
        authed.delete(f"/api/library/models/{clean['id']}")


def test_viewers_can_read_the_hints(authed, model):
    from app.main import app
    _log(authed, model, outcome="failed", failure_reason="power")
    authed.post("/api/users", json={"username": "hintviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "hintviewer", "password": PW})
        assert str(model["id"]) in viewer.get("/api/prints/hints").json()["hints"]
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert TestClient(app).get("/api/prints/hints").status_code == 401
