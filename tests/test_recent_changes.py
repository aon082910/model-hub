"""The activity history and undoing bulk edits."""
import uuid

import pytest
from starlette.testclient import TestClient

from app import activity

PW = "a long enough password"


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def pair(authed):
    tag = uuid.uuid4().hex[:5]
    a = authed.post("/api/library/import", files={"file": (f"{tag}_a.stl", _stl(41 + int(tag[:2], 16) % 40), "application/octet-stream")}).json()
    b = authed.post("/api/library/import", files={"file": (f"{tag}_b.stl", _stl(101 + int(tag[:2], 16) % 40), "application/octet-stream")}).json()
    yield tag, a, b
    for m in (a, b):
        authed.delete(f"/api/library/models/{m['id']}")


def _latest(c, **params):
    return c.get("/api/activity", params={"limit": 5, **params}).json()["items"][0]


def _model(c, m):
    return c.get(f"/api/library/models/{m['id']}").json()


def test_a_bulk_edit_is_recorded_and_can_be_undone(authed, pair):
    tag, a, b = pair
    r = authed.post("/api/bulk", json={"action": "add_tag", "value": "undo-me", "ids": [a["id"], b["id"]]}).json()
    assert r["changed"] == 2 and r["activity_id"]
    entry = _latest(authed)
    assert entry["id"] == r["activity_id"] and entry["action"] == "bulk_edit" and entry["undoable"] is True
    assert "undo-me" in entry["summary"] and "2 models" in entry["summary"]
    assert entry["actor"] == "fixture-admin" or entry["actor"]
    assert [t["name"] for t in _model(authed, a)["tags"]] == ["undo-me"]
    undone = authed.post(f"/api/activity/{entry['id']}/undo").json()
    assert undone == {"restored": 2}
    assert _model(authed, a)["tags"] == [] and _model(authed, b)["tags"] == []
    after = authed.get("/api/activity", params={"limit": 5}).json()["items"]
    assert after[0]["action"] == "undo" and after[1]["undone"] is True and after[1]["undoable"] is False
    assert authed.post(f"/api/activity/{entry['id']}/undo").status_code == 409                   # not twice


@pytest.mark.parametrize("action,value_kind", [("add_collection", "collection"), ("add_project", "project")])
def test_collection_and_project_additions_undo(authed, pair, action, value_kind):
    tag, a, b = pair
    made = authed.post("/api/collections" if value_kind == "collection" else "/api/projects", json={"name": f"undo {value_kind} {tag}"}).json()
    try:
        authed.post("/api/bulk", json={"action": action, "value": made["id"], "ids": [a["id"]]})
        key = "collection_id" if value_kind == "collection" else "project_id"
        assert [m["id"] for m in authed.get("/api/library/models", params={key: made["id"]}).json()] == [a["id"]]
        authed.post(f"/api/activity/{_latest(authed)['id']}/undo")
        assert authed.get("/api/library/models", params={key: made["id"]}).json() == []
    finally:
        authed.delete(f"/api/{'collections' if value_kind == 'collection' else 'projects'}/{made['id']}")


def test_removing_a_tag_undoes_to_adding_it_back(authed, pair):
    tag, a, b = pair
    authed.post(f"/api/tags/models/{a['id']}", json={"name": "keepme"})
    authed.post("/api/bulk", json={"action": "remove_tag", "value": "keepme", "ids": [a["id"], b["id"]]})
    assert _model(authed, a)["tags"] == []
    authed.post(f"/api/activity/{_latest(authed)['id']}/undo")
    assert [t["name"] for t in _model(authed, a)["tags"]] == ["keepme"] and _model(authed, b)["tags"] == []     # only a had it


def test_designer_changes_restore_the_old_values_but_not_over_newer_edits(authed, pair):
    tag, a, b = pair
    authed.patch(f"/api/library/models/{a['id']}", json={"designer": "Original One"})
    authed.post("/api/bulk", json={"action": "set_designer", "value": "Bulk Person", "ids": [a["id"], b["id"]]})
    entry_id = _latest(authed)["id"]
    authed.patch(f"/api/library/models/{b['id']}", json={"designer": "Edited Later"})                 # changed again since
    assert authed.post(f"/api/activity/{entry_id}/undo").json() == {"restored": 1}
    assert _model(authed, a)["designer"] == "Original One" and _model(authed, b)["designer"] == "Edited Later"


def test_queueing_undoes_by_removing_the_new_entries_that_have_not_started(authed, pair):
    tag, a, b = pair
    before = len(authed.get("/api/queue").json())
    authed.post("/api/bulk", json={"action": "queue", "ids": [a["id"], b["id"]]})
    queue = authed.get("/api/queue").json()
    started = next(q for q in queue if q["model_id"] == a["id"])
    authed.patch(f"/api/queue/{started['id']}", json={"status": "printing"})
    assert authed.post(f"/api/activity/{_latest(authed)['id']}/undo").json() == {"restored": 1}     # the one that began stays
    assert len(authed.get("/api/queue").json()) == before + 1
    authed.delete(f"/api/queue/{started['id']}")


def test_a_bulk_edit_that_changed_nothing_is_not_recorded(authed, pair):
    tag, a, b = pair
    authed.post("/api/bulk", json={"action": "add_tag", "value": "same-twice", "ids": [a["id"]]})
    total = authed.get("/api/activity").json()["total"]
    r = authed.post("/api/bulk", json={"action": "add_tag", "value": "same-twice", "ids": [a["id"]]}).json()
    assert r["changed"] == 0 and r["activity_id"] is None
    assert authed.get("/api/activity").json()["total"] == total


# ---------- other things that are recorded ----------

def test_logins_tokens_shares_and_printers_are_recorded_without_secrets(authed, pair):
    tag, a, b = pair
    authed.post("/api/users", json={"username": "histuser", "password": "super secret password 1", "role": "viewer"})
    uid = authed.get("/api/users").json()["users"][0]["id"]
    authed.patch(f"/api/users/{uid}", json={"password": "another secret password 2", "role": "member"})
    authed.delete(f"/api/users/{uid}")
    t = authed.post("/api/tokens", json={"name": "histtoken", "scope": "write"}).json()
    authed.delete(f"/api/tokens/{t['id']}")
    s = authed.post("/api/shares", json={"kind": "model", "target_id": a["id"]}).json()
    authed.delete(f"/api/shares/{s['id']}")
    p = authed.post("/api/printers", json={"name": "Histprinter", "kind": "octoprint", "url": "http://octopi.local", "api_key": "printer-key-secret"}).json()
    authed.delete(f"/api/printers/{p['id']}")
    authed.put("/api/settings", json={"est_material": "ABS", "thingiverse_token": "tv-secret-token"})
    authed.put("/api/settings", json={"est_material": "", "thingiverse_token": ""})
    items = authed.get("/api/activity", params={"limit": 60}).json()["items"]
    text = " | ".join(i["summary"] for i in items)
    for expected in ("Added the viewer login histuser", "password reset", "role to member", "Removed the login histuser", "Made the write API token 'histtoken'",
                     "Revoked the API token 'histtoken'", "Made a share link for the model", "Stopped sharing the model", "Added the printer Histprinter",
                     "Removed the printer Histprinter", "Changed settings: est_material, thingiverse_token"):
        assert expected in text, expected
    for secret in ("super secret password 1", "another secret password 2", "printer-key-secret", "tv-secret-token", t["token"], s["path"].split("/")[-1], "ABS"):
        assert secret not in text, secret


def test_removing_a_model_and_cleaning_duplicates_are_recorded(authed):
    n = 201 + uuid.uuid4().int % 50
    a = authed.post("/api/library/import", files={"file": (f"dupe_hist_a_{n}.stl", _stl(n), "application/octet-stream")}).json()
    b = authed.post("/api/library/import", files={"file": (f"dupe_hist_b_{n}.stl", _stl(n), "application/octet-stream")}).json()
    authed.post("/api/duplicates/merge", json={"keep_id": a["id"], "remove_ids": [b["id"]]})
    assert "Cleaned up duplicates" in _latest(authed)["summary"]
    authed.delete(f"/api/library/models/{a['id']}")
    entry = _latest(authed)
    assert entry["action"] == "remove_model" and a["filename"] in entry["summary"] and entry["undoable"] is False
    assert authed.post(f"/api/activity/{entry['id']}/undo").status_code == 409
    authed.delete(f"/api/library/models/{b['id']}")


def test_a_restore_is_recorded(authed):
    data = authed.get("/api/backup").content
    authed.post("/api/backup/restore", files={"file": ("b.zip", data)}, data={"confirm": "replace"})
    assert _latest(authed)["action"] == "restore"
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


# ---------- listing, access, limits ----------

def test_listing_filters_and_paging(authed, pair):
    tag, a, b = pair
    authed.post("/api/bulk", json={"action": "add_tag", "value": "filter-me", "ids": [a["id"]]})
    page = authed.get("/api/activity", params={"action": "bulk_edit", "limit": 1}).json()
    assert len(page["items"]) == 1 and page["items"][0]["action"] == "bulk_edit" and page["total"] >= 1 and page["actors"]
    mine = authed.get("/api/activity", params={"actor": page["items"][0]["actor"], "limit": 200}).json()
    assert all(i["actor"] == page["items"][0]["actor"] for i in mine["items"])
    assert authed.get("/api/activity", params={"limit": 0}).status_code == 422
    assert authed.get("/api/activity", params={"limit": 1000}).status_code == 422


def test_members_can_undo_but_viewers_cannot_and_anonymous_cannot_look(authed, pair):
    tag, a, b = pair
    authed.post("/api/bulk", json={"action": "add_tag", "value": "access-check", "ids": [a["id"]]})
    entry_id = _latest(authed)["id"]
    authed.post("/api/users", json={"username": "histviewer", "password": PW, "role": "viewer"})
    authed.post("/api/users", json={"username": "histmember", "password": PW, "role": "member"})
    from app.main import app
    try:
        viewer, member = TestClient(app), TestClient(app)
        viewer.post("/api/auth/login", json={"username": "histviewer", "password": PW})
        member.post("/api/auth/login", json={"username": "histmember", "password": PW})
        assert viewer.get("/api/activity").status_code == 200
        assert viewer.post(f"/api/activity/{entry_id}/undo").status_code == 403
        assert member.post(f"/api/activity/{entry_id}/undo").status_code == 200
        assert _latest(authed)["actor"] == "histmember"                                       # the undo is attributed
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert TestClient(app).get("/api/activity").status_code == 401
    assert authed.post("/api/activity/987654/undo").status_code == 404


def test_old_entries_are_trimmed(authed, monkeypatch):
    from sqlmodel import Session
    from app.db import engine
    monkeypatch.setattr(activity, "MAX_ENTRIES", 20)
    with Session(engine) as s:
        for i in range(125):
            activity.record(s, "trimmer", "test", f"entry {i}")
        from sqlmodel import select
        from app.models import ActivityLog
        remaining = len(s.exec(select(ActivityLog.id)).all())
    assert remaining <= 20 + 100
    newest = authed.get("/api/activity", params={"actor": "trimmer", "limit": 1}).json()["items"][0]
    assert newest["summary"] == "entry 124"
