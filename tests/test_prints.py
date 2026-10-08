"""The print log: entries, the filament they take and give back, photos, the library filters, and the queue."""
import io
import zipfile

import pytest
from PIL import Image

from app.routers import prints as prints_module


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def model(authed):
    name = f"printlog_{len(authed.get('/api/library/models').json()) + 1000}.stl"
    r = authed.post("/api/library/import", files={"file": (name, _stl(700 + len(name)), "application/octet-stream")})
    assert r.status_code == 200, r.text
    m = r.json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


@pytest.fixture()
def spool(authed):
    s = authed.post("/api/filament", json={"material": "PLA", "color": "log-test", "spool_weight_g": 1000, "remaining_g": 500}).json()
    yield s
    authed.delete(f"/api/filament/{s['id']}")


def _remaining(c, spool_id):
    return next(f for f in c.get("/api/filament").json() if f["id"] == spool_id)["remaining_g"]


def _jpeg(size=(40, 30)):
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, "JPEG")
    return out.getvalue()


def test_a_print_is_logged_and_takes_filament(authed, model, spool):
    r = authed.post("/api/prints", json={"model_id": model["id"], "filament_id": spool["id"], "grams": 120.5, "minutes": 95,
                                         "rating": 4, "notes": "  came out well ", "printed_at": "2026-09-01"})
    assert r.status_code == 200, r.text
    log = r.json()
    assert log["rating"] == 4 and log["notes"] == "came out well" and log["printed_at"].startswith("2026-09-01")
    assert log["deducted_g"] == 120.5 and log["model_filename"] == model["filename"] and log["has_photo"] is False
    assert _remaining(authed, spool["id"]) == 379.5


def test_deducting_is_optional_and_never_goes_below_zero(authed, model, spool):
    authed.post("/api/prints", json={"model_id": model["id"], "filament_id": spool["id"], "grams": 50, "deduct": False})
    assert _remaining(authed, spool["id"]) == 500
    big = authed.post("/api/prints", json={"model_id": model["id"], "filament_id": spool["id"], "grams": 800}).json()
    assert big["deducted_g"] == 500 and _remaining(authed, spool["id"]) == 0


def test_deleting_an_entry_gives_back_exactly_what_it_took(authed, model, spool):
    big = authed.post("/api/prints", json={"model_id": model["id"], "filament_id": spool["id"], "grams": 800}).json()
    assert _remaining(authed, spool["id"]) == 0
    assert authed.delete(f"/api/prints/{big['id']}").status_code == 200
    assert _remaining(authed, spool["id"]) == 500                   # the 500 g it really took, not the 800 g it claimed
    assert authed.delete(f"/api/prints/{big['id']}").status_code == 404


def test_editing_grams_or_spool_keeps_the_spool_level_right(authed, model, spool):
    other = authed.post("/api/filament", json={"material": "PETG", "color": "log-test-2", "remaining_g": 300}).json()
    try:
        log = authed.post("/api/prints", json={"model_id": model["id"], "filament_id": spool["id"], "grams": 100}).json()
        assert _remaining(authed, spool["id"]) == 400
        authed.patch(f"/api/prints/{log['id']}", json={"grams": 60})
        assert _remaining(authed, spool["id"]) == 440
        authed.patch(f"/api/prints/{log['id']}", json={"filament_id": other["id"]})
        assert _remaining(authed, spool["id"]) == 500 and _remaining(authed, other["id"]) == 240
        authed.patch(f"/api/prints/{log['id']}", json={"rating": 5, "notes": "great", "minutes": 30})
        assert _remaining(authed, other["id"]) == 240                # unrelated edits leave the spool alone
        shown = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]
        assert shown["rating"] == 5 and shown["notes"] == "great" and shown["minutes"] == 30
    finally:
        authed.delete(f"/api/filament/{other['id']}")


@pytest.mark.parametrize("payload,fragment", [
    ({"rating": 9}, "rating"), ({"rating": "x"}, "rating"), ({"grams": -1}, "grams"), ({"minutes": "soon"}, "minutes"),
    ({"printed_at": "yesterday"}, "printed_at"), ({"filament_id": 987654}, "spool"), ({"notes": 5}, "notes"),
])
def test_bad_values_are_refused(authed, model, payload, fragment):
    r = authed.post("/api/prints", json={"model_id": model["id"], **payload})
    assert r.status_code == 400 and fragment in r.json()["detail"]
    assert authed.get("/api/prints", params={"model_id": model["id"]}).json()["total"] == 0


def test_unknown_model_is_refused(authed):
    assert authed.post("/api/prints", json={"model_id": 99999999}).status_code == 404
    assert authed.post("/api/prints", json={}).status_code == 404


def test_photos_are_shrunk_stored_and_removed(authed, model, tmp_path):
    log = authed.post("/api/prints", json={"model_id": model["id"]}).json()
    big = Image.new("RGB", (3000, 2000), (10, 120, 200))
    buffer = io.BytesIO()
    big.save(buffer, "PNG")
    r = authed.post(f"/api/prints/{log['id']}/photo", files={"file": ("p.png", buffer.getvalue(), "image/png")})
    assert r.status_code == 200
    got = authed.get(f"/api/prints/{log['id']}/photo")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(got.content)).size) <= 1200
    assert authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"][0]["has_photo"] is True
    assert authed.delete(f"/api/prints/{log['id']}/photo").status_code == 200
    assert authed.get(f"/api/prints/{log['id']}/photo").status_code == 404


def test_a_photo_that_is_not_a_picture_is_refused(authed, model):
    log = authed.post("/api/prints", json={"model_id": model["id"]}).json()
    r = authed.post(f"/api/prints/{log['id']}/photo", files={"file": ("p.jpg", b"not a picture", "image/jpeg")})
    assert r.status_code == 400
    assert authed.post("/api/prints/999999/photo", files={"file": ("p.jpg", _jpeg(), "image/jpeg")}).status_code == 404


def test_library_shows_print_counts_and_filters(authed, model):
    assert authed.get(f"/api/library/models/{model['id']}").json()["print_count"] == 0
    authed.post("/api/prints", json={"model_id": model["id"], "printed_at": "2026-01-02"})
    authed.post("/api/prints", json={"model_id": model["id"], "printed_at": "2026-03-04"})
    shown = authed.get(f"/api/library/models/{model['id']}").json()
    assert shown["print_count"] == 2 and shown["last_printed_at"].startswith("2026-03-04")
    printed = {m["id"] for m in authed.get("/api/library/models", params={"printed": "true", "limit": 5000}).json()}
    never = {m["id"] for m in authed.get("/api/library/models", params={"printed": "false", "limit": 5000}).json()}
    assert model["id"] in printed and model["id"] not in never
    assert not printed & never


def test_finishing_a_queue_job_logs_the_print_once(authed, model, spool):
    item = authed.post("/api/queue", json={"model_id": model["id"], "filament_id": spool["id"], "estimated_grams": 40,
                                           "estimated_minutes": 61}).json()
    authed.patch(f"/api/queue/{item['id']}", json={"status": "done"})
    logs = authed.get("/api/prints", params={"model_id": model["id"]}).json()["items"]
    assert len(logs) == 1 and logs[0]["source"] == "queue" and logs[0]["grams"] == 40 and logs[0]["minutes"] == 61
    assert _remaining(authed, spool["id"]) == 460                    # taken once, by the queue
    authed.patch(f"/api/queue/{item['id']}", json={"status": "queued"})
    authed.patch(f"/api/queue/{item['id']}", json={"status": "done"})
    assert authed.get("/api/prints", params={"model_id": model["id"]}).json()["total"] == 1
    authed.delete(f"/api/queue/{item['id']}")


def test_removing_a_model_removes_its_log_and_photos(authed, spool):
    r = authed.post("/api/library/import", files={"file": ("printlog_gone.stl", _stl(777), "application/octet-stream")}).json()
    log = authed.post("/api/prints", json={"model_id": r["id"], "filament_id": spool["id"], "grams": 10}).json()
    authed.post(f"/api/prints/{log['id']}/photo", files={"file": ("p.jpg", _jpeg(), "image/jpeg")})
    assert prints_module.photo_path(log["id"]).is_file()
    authed.delete(f"/api/library/models/{r['id']}")
    assert authed.get("/api/prints", params={"model_id": r["id"]}).json()["total"] == 0
    assert not prints_module.photo_path(log["id"]).exists()


def test_print_photos_are_part_of_a_backup(authed, model):
    log = authed.post("/api/prints", json={"model_id": model["id"]}).json()
    authed.post(f"/api/prints/{log['id']}/photo", files={"file": ("p.jpg", _jpeg(), "image/jpeg")})
    data = authed.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert f"print_photos/{log['id']}.jpg" in z.namelist()
    prints_module.photo_path(log["id"]).unlink()
    assert authed.post("/api/backup/restore", files={"file": ("b.zip", data)}, data={"confirm": "replace"}).status_code == 200
    assert prints_module.photo_path(log["id"]).is_file()
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


def test_the_print_log_needs_a_login(authed):
    authed.post("/api/auth/logout")
    try:
        assert authed.get("/api/prints").status_code == 401
        assert authed.post("/api/prints", json={"model_id": 1}).status_code == 401
    finally:
        from conftest import ensure_authenticated
        ensure_authenticated(authed)
