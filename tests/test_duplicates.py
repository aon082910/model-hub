"""Cleaning up identical copies: suggestions, carrying work over, and never deleting anything that is not a true copy."""
import os
import uuid

import pytest

from app.config import LIBRARY_PATH


def _stl(n, pad=""):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            f" endloop\nendfacet\nendsolid t\n{pad}").encode()


def _import(c, name, content):
    r = c.post("/api/library/import", files={"file": (name, content, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def pair(authed):
    """Two imported files with identical contents; removed again afterwards."""
    n = 300 + (uuid.uuid4().int % 500)
    content = _stl(n)
    a = _import(authed, f"dupe_a_{n}.stl", content)
    b = _import(authed, f"dupe_b_{n}.stl", content)
    assert a["id"] != b["id"]
    yield a, b, content
    for m in (a, b):
        authed.delete(f"/api/library/models/{m['id']}")


def _group_with(c, model_id):
    for g in c.get("/api/duplicates", params={"limit": 200}).json()["groups"]:
        if model_id in {m["id"] for m in g["models"]}:
            return g
    return None


def _exists(model):
    return (LIBRARY_PATH / model["path"]).is_file()


def test_identical_files_are_grouped_with_a_suggestion(authed, pair):
    a, b, _ = pair
    authed.post(f"/api/tags/models/{b['id']}", json={"name": "dupe-keeper"})
    g = _group_with(authed, a["id"])
    assert g and {m["id"] for m in g["models"]} == {a["id"], b["id"]}
    assert g["suggested_keep"] == b["id"]                       # the one with your tag on it
    row = next(m for m in g["models"] if m["id"] == b["id"])
    assert row["tags"] == 1 and row["file_exists"] is True and g["reclaimable_bytes"] == a["size_bytes"]


def test_merge_without_deleting_shares_the_work_and_keeps_every_file(authed, pair):
    a, b, _ = pair
    authed.post(f"/api/tags/models/{b['id']}", json={"name": "dupe-shared"})
    authed.patch(f"/api/library/models/{b['id']}", json={"notes": "b's notes", "designer": "Dupe Designer"})
    col = authed.post("/api/collections", json={"name": "dupe-collection"}).json()
    authed.post(f"/api/collections/{col['id']}/models/{b['id']}")
    r = authed.post("/api/duplicates/merge", json={"keep_id": a["id"], "remove_ids": [b["id"]]})
    assert r.status_code == 200, r.text
    assert r.json() == {"merged": 1, "deleted_files": 0, "removed_records": 0, "freed_bytes": 0, "skipped": []}
    kept = authed.get(f"/api/library/models/{a['id']}/full").json()
    assert [t["name"] for t in kept["tags"]] == ["dupe-shared"] and [c["name"] for c in kept["collections"]] == ["dupe-collection"]
    assert kept["notes"] == "b's notes" and kept["designer"] == "Dupe Designer"
    assert _exists(a) and _exists(b)
    authed.delete(f"/api/collections/{col['id']}")


def test_merge_with_deleting_removes_the_copy_and_hands_everything_over(authed, pair):
    a, b, _ = pair
    spool = authed.post("/api/filament", json={"material": "PLA", "remaining_g": 200}).json()
    authed.post("/api/prints", json={"model_id": b["id"], "filament_id": spool["id"], "grams": 20})
    queued = authed.post("/api/queue", json={"model_id": b["id"]}).json()
    project = authed.post("/api/projects", json={"name": "dupe project"}).json()
    authed.post(f"/api/projects/{project['id']}/models/{b['id']}")
    authed.post(f"/api/tags/models/{b['id']}", json={"name": "dupe-moved"})
    try:
        r = authed.post("/api/duplicates/merge", json={"keep_id": a["id"], "remove_ids": [b["id"]], "delete_files": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["merged"] == 1 and body["deleted_files"] == 1 and body["removed_records"] == 1
        assert body["freed_bytes"] == b["size_bytes"] and body["skipped"] == []
        assert _exists(a) and not _exists(b)
        assert authed.get(f"/api/library/models/{b['id']}").status_code == 404
        kept = authed.get(f"/api/library/models/{a['id']}/full").json()
        assert [t["name"] for t in kept["tags"]] == ["dupe-moved"]
        assert [p["id"] for p in kept["projects"]] == [project["id"]]
        assert [q["id"] for q in kept["queue"]] == [queued["id"]]           # the queue entry moved with it
        assert authed.get("/api/prints", params={"model_id": a["id"]}).json()["total"] == 1   # and the print log
    finally:
        authed.delete(f"/api/projects/{project['id']}")
        authed.delete(f"/api/queue/{queued['id']}")
        authed.delete(f"/api/filament/{spool['id']}")


def test_a_file_that_changed_since_the_scan_is_never_deleted(authed, pair):
    a, b, _ = pair
    with open(LIBRARY_PATH / b["path"], "ab") as f:
        f.write(b"\n# edited since the scan\n")
    r = authed.post("/api/duplicates/merge", json={"keep_id": a["id"], "remove_ids": [b["id"]], "delete_files": True}).json()
    assert r["deleted_files"] == 0 and r["removed_records"] == 0
    assert r["skipped"][0]["id"] == b["id"] and "changed" in r["skipped"][0]["reason"]
    assert _exists(b) and authed.get(f"/api/library/models/{b['id']}").status_code == 200


def test_nothing_is_deleted_when_the_kept_file_is_missing(authed, pair):
    a, b, _ = pair
    os.remove(LIBRARY_PATH / a["path"])
    r = authed.post("/api/duplicates/merge", json={"keep_id": a["id"], "remove_ids": [b["id"]], "delete_files": True}).json()
    assert r["deleted_files"] == 0 and "missing" in r["skipped"][0]["reason"]
    assert _exists(b)


def test_a_path_outside_the_library_is_never_deleted(authed, pair):
    from sqlmodel import Session
    from app.db import engine
    from app.models import Model3D
    a, b, content = pair
    outside = LIBRARY_PATH.parent / "outside_dupe.stl"
    outside.write_bytes(content)
    with Session(engine) as s:
        row = s.get(Model3D, b["id"])
        row.path = "../outside_dupe.stl"
        s.add(row)
        s.commit()
    try:
        r = authed.post("/api/duplicates/merge", json={"keep_id": a["id"], "remove_ids": [b["id"]], "delete_files": True}).json()
        assert r["deleted_files"] == 0 and "outside" in r["skipped"][0]["reason"]
        assert outside.is_file()
    finally:
        outside.unlink(missing_ok=True)


def test_only_exact_copies_can_be_merged(authed, pair):
    a, _, _ = pair
    other = _import(authed, "dupe_other.stl", _stl(1234))
    try:
        r = authed.post("/api/duplicates/merge", json={"keep_id": a["id"], "remove_ids": [other["id"]], "delete_files": True})
        assert r.status_code == 400 and "exact copies" in r.json()["detail"]
        assert _exists(other)
    finally:
        authed.delete(f"/api/library/models/{other['id']}")


@pytest.mark.parametrize("payload", [{}, {"keep_id": "x"}, {"keep_id": 1}, {"keep_id": 1, "remove_ids": [1]},
                                     {"keep_id": 1, "remove_ids": ["2"]}, {"keep_id": 987654, "remove_ids": [1]}])
def test_bad_merge_requests_are_refused(authed, payload):
    assert authed.post("/api/duplicates/merge", json=payload).status_code == 400


def test_merge_all_needs_confirmation_to_delete(authed, pair):
    a, b, _ = pair
    assert authed.post("/api/duplicates/merge-all", json={"delete_files": True}).status_code == 400
    r = authed.post("/api/duplicates/merge-all", json={"delete_files": True, "confirm": "delete"})
    assert r.status_code == 200, r.text
    assert r.json()["deleted_files"] >= 1 and r.json()["freed_bytes"] >= b["size_bytes"]
    present = [m for m in (a, b) if _exists(m)]
    assert len(present) == 1                                            # exactly one copy is left


def test_merge_all_without_deleting_only_shares_information(authed, pair):
    a, b, _ = pair
    authed.post(f"/api/tags/models/{a['id']}", json={"name": "dupe-all-tag"})
    authed.patch(f"/api/library/models/{b['id']}", json={"notes": "shared by merge-all"})
    r = authed.post("/api/duplicates/merge-all", json={}).json()
    assert r["deleted_files"] == 0 and r["removed_records"] == 0 and r["merged"] >= 1
    assert _exists(a) and _exists(b)
    both = [m for m in (authed.get(f"/api/library/models/{i}").json() for i in (a["id"], b["id"]))
            if "shared by merge-all" in (m["notes"] or "") and [t["name"] for t in m["tags"]] == ["dupe-all-tag"]]
    assert len(both) >= 1                                       # the model that was kept now has both


def test_same_shape_but_different_bytes_is_only_listed(authed):
    n = 900 + (uuid.uuid4().int % 100)
    a = _import(authed, f"similar_a_{n}.stl", _stl(n))
    b = _import(authed, f"similar_b_{n}.stl", _stl(n, pad="\n\n\n"))       # same triangles, different file
    try:
        assert a["content_hash"] != b["content_hash"]
        groups = authed.get("/api/duplicates/similar").json()["groups"]
        ids = [{m["id"] for m in g} for g in groups]
        assert {a["id"], b["id"]} in ids
        assert _group_with(authed, a["id"]) is None                 # not an exact duplicate
        r = authed.post("/api/duplicates/merge", json={"keep_id": a["id"], "remove_ids": [b["id"]], "delete_files": True})
        assert r.status_code == 400
    finally:
        authed.delete(f"/api/library/models/{a['id']}")
        authed.delete(f"/api/library/models/{b['id']}")


def test_duplicates_need_a_login(authed):
    authed.post("/api/auth/logout")
    try:
        assert authed.get("/api/duplicates").status_code == 401
        assert authed.post("/api/duplicates/merge", json={}).status_code == 401
    finally:
        from conftest import ensure_authenticated
        ensure_authenticated(authed)
