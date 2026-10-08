"""Exporting the library's information, and importing it (or a spreadsheet from another tool)."""
import csv
import io
import json
import uuid

import pytest
from starlette.testclient import TestClient

PW = "a long enough password"


def _stl(n, salt="t"):
    """The salt makes each model's content (and so its hash) its own, whatever the other tests left in the library."""
    return (f"solid {salt}\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def trio(authed):
    tag = uuid.uuid4().hex[:6]
    models = []
    for i, n in enumerate((44, 55, 66)):
        models.append(authed.post("/api/library/import", files={"file": (f"{tag}_part{i}.stl", _stl(n + int(tag[:2], 16) % 20, f"{tag}{i}"), "application/octet-stream")}).json())
    yield tag, models
    for m in models:
        authed.delete(f"/api/library/models/{m['id']}")


def _upload(c, name, data, **form):
    return c.post("/api/library-io/import", files={"file": (name, data, "application/octet-stream")}, data={k: str(v).lower() if isinstance(v, bool) else v for k, v in form.items()})


def _model(c, m):
    return c.get(f"/api/library/models/{m['id']}").json()


def test_json_export_has_the_information_not_the_files(authed, trio):
    tag, (a, b, c) = trio
    authed.patch(f"/api/library/models/{a['id']}", json={"designer": "Dana", "license": "CC0", "notes": "keep this"})
    authed.post("/api/bulk", json={"action": "add_tag", "value": "exported", "ids": [a["id"]]})
    col = authed.post("/api/collections", json={"name": f"exp col {tag}"}).json()
    authed.post(f"/api/collections/{col['id']}/models/{a['id']}")
    authed.post("/api/prints", json={"model_id": a["id"]})
    try:
        r = authed.get("/api/library-io/export.json")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and ".json" in r.headers["content-disposition"]
        doc = r.json()
        assert doc["format"] == "modelhub-library" and doc["version"] == 1
        row = next(m for m in doc["models"] if m["filename"] == a["filename"])
        assert row["designer"] == "Dana" and row["license"] == "CC0" and row["notes"] == "keep this" and row["tags"] == ["exported"]
        assert row["collections"] == [f"exp col {tag}"] and row["print_count"] == 1 and row["content_hash"] == a["content_hash"]
        assert "solid t" not in r.text                                                    # no file contents
    finally:
        authed.delete(f"/api/collections/{col['id']}")


def test_csv_export_is_safe_for_spreadsheets(authed, trio):
    tag, (a, b, c) = trio
    authed.patch(f"/api/library/models/{a['id']}", json={"designer": "=HYPERLINK(\"http://evil\")", "notes": "+cmd"})
    text = authed.get("/api/library-io/export.csv").text
    rows = list(csv.DictReader(io.StringIO(text)))
    mine = next(r for r in rows if r["filename"] == a["filename"])
    assert mine["designer"].startswith("'=") and mine["notes"].startswith("'+")
    assert list(rows[0].keys())[:3] == ["path", "filename", "tags"]


def test_an_export_restores_what_was_removed(authed, trio):
    tag, (a, b, c) = trio
    authed.patch(f"/api/library/models/{b['id']}", json={"designer": "Original", "license": "CC-BY"})
    authed.post("/api/bulk", json={"action": "add_tag", "value": f"keep-{tag}", "ids": [a["id"], b["id"]]})
    col = authed.post("/api/collections", json={"name": f"roundtrip {tag}"}).json()
    authed.post(f"/api/collections/{col['id']}/models/{b['id']}")
    exported = authed.get("/api/library-io/export.json").content
    try:
        authed.post("/api/bulk", json={"action": "remove_tag", "value": f"keep-{tag}", "ids": [a["id"], b["id"]]})
        authed.delete(f"/api/collections/{col['id']}/models/{b['id']}")
        authed.patch(f"/api/library/models/{b['id']}", json={"designer": "", "license": ""})
        r = _upload(authed, "export.json", exported)
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["matched"] >= 3 and result["tags_added"] >= 2 and result["collections_added"] >= 1 and result["fields_set"] >= 2
        assert [t["name"] for t in _model(authed, a)["tags"]] == [f"keep-{tag}"]
        assert _model(authed, b)["designer"] == "Original" and _model(authed, b)["license"] == "CC-BY"
        assert [m["id"] for m in authed.get("/api/library/models", params={"collection_id": col["id"]}).json()] == [b["id"]]
        again = _upload(authed, "export.json", exported).json()
        assert again["tags_added"] == 0 and again["collections_added"] == 0 and again["models_changed"] == 0         # nothing twice
    finally:
        authed.delete(f"/api/collections/{col['id']}")


def test_a_dry_run_changes_nothing(authed, trio):
    tag, (a, b, c) = trio
    sheet = f"filename,tags,collections,designer\n{a['filename']},dry-tag; second,Dry Collection {tag},Someone\n"
    r = _upload(authed, "sheet.csv", sheet.encode(), dry_run=True).json()
    assert r["dry_run"] is True and r["matched"] == 1 and r["tags_added"] == 2 and r["collections_added"] == 1 and r["fields_set"] == 1
    assert _model(authed, a)["tags"] == [] and _model(authed, a)["designer"] is None
    assert not [c for c in authed.get("/api/collections").json() if c["name"] == f"Dry Collection {tag}"]


def test_a_spreadsheet_from_another_tool_is_matched_by_name_or_path(authed, trio):
    tag, (a, b, c) = trio
    sheet = (f"File Name,Labels,Author,Licence,Comments\n{a['filename']},\"vase, spiral\",Pat,MIT,first note\n"
             f"{b['filename'].upper()},mechanical,,,\n")
    r = _upload(authed, "other-tool.csv", sheet.encode()).json()
    assert r["matched"] == 2 and r["unmatched"] == 0
    got = _model(authed, a)
    assert sorted(t["name"] for t in got["tags"]) == ["spiral", "vase"] and got["designer"] == "Pat" and got["license"] == "MIT" and got["notes"] == "first note"
    assert [t["name"] for t in _model(authed, b)["tags"]] == ["mechanical"]
    by_path = _upload(authed, "p.csv", f"path,tags\n{c['path']},bypath\n".encode()).json()
    assert by_path["matched"] == 1 and [t["name"] for t in _model(authed, c)["tags"]] == ["bypath"]


def test_existing_values_are_kept_unless_overwrite_is_asked_for(authed, trio):
    tag, (a, b, c) = trio
    authed.patch(f"/api/library/models/{a['id']}", json={"designer": "Keep Me", "notes": "my own notes"})
    sheet = f"filename,designer,notes\n{a['filename']},New Person,new notes\n".encode()
    assert _upload(authed, "s.csv", sheet).json()["fields_set"] == 0
    assert _model(authed, a)["designer"] == "Keep Me"
    assert _upload(authed, "s.csv", sheet, overwrite=True).json()["fields_set"] == 2
    got = _model(authed, a)
    assert got["designer"] == "New Person" and got["notes"] == "new notes"


def test_the_content_hash_wins_over_a_wrong_name(authed, trio):
    tag, (a, b, c) = trio
    rows = {"format": "modelhub-library", "version": 1, "models": [{"filename": "totally-wrong.stl", "path": "nowhere/x.stl", "content_hash": a["content_hash"], "tags": ["by-hash"]}]}
    r = _upload(authed, "x.json", json.dumps(rows).encode()).json()
    assert r["matched"] == 1 and [t["name"] for t in _model(authed, a)["tags"]] == ["by-hash"]


def test_unknown_and_ambiguous_rows_are_reported(authed, trio):
    tag, (a, b, c) = trio
    twin = authed.post("/api/library/import", files={"file": (a["filename"].replace("_part0", "_twin"), _stl(171), "application/octet-stream")}).json()
    dup_name = authed.post("/api/library/import", files={"file": (b["filename"], _stl(188), "application/octet-stream")}).json()   # same name, renamed on import
    try:
        sheet = "filename,tags\nnot-in-this-library.stl,x\n,y\n"
        r = _upload(authed, "s.csv", sheet.encode()).json()
        assert r["matched"] == 0 and r["unmatched"] == 2 and "no model matched" in r["problems"][0]
    finally:
        authed.delete(f"/api/library/models/{twin['id']}")
        authed.delete(f"/api/library/models/{dup_name['id']}")


def test_projects_are_joined_but_never_created_and_listings_need_a_real_id(authed, trio):
    tag, (a, b, c) = trio
    project = authed.post("/api/projects", json={"name": f"Existing project {tag}"}).json()
    try:
        sheet = (f"filename,projects,source_provider,source_id,source_url,source_title\n"
                 f"{a['filename']},Existing project {tag}; Invented project,printables,3161,https://www.printables.com/model/3161,3D Benchy\n"
                 f"{b['filename']},,printables,not-a-number,https://evil.example/,Bad\n")
        r = _upload(authed, "s.csv", sheet.encode()).json()
        assert r["projects_added"] == 1 and r["sources_set"] == 1
        assert [p["name"] for p in authed.get("/api/projects").json() if p["name"] == "Invented project"] == []
        got = _model(authed, a)
        assert (got["source_provider"], got["source_id"], got["source_title"]) == ("printables", "3161", "3D Benchy")
        assert _model(authed, b)["source_provider"] is None
        again = _upload(authed, "s.csv", sheet.encode()).json()
        assert again["sources_set"] == 0                                             # a model that is linked keeps its link
    finally:
        authed.delete(f"/api/library/models/{a['id']}/source")
        authed.delete(f"/api/projects/{project['id']}")


@pytest.mark.parametrize("name,data,fragment", [
    ("bad.json", b"{not json", "valid JSON"), ("list.json", b'{"models": 5}', "no list"), ("other.json", b'{"format": "something-else", "models": []}', "not a Model Hub export"),
    ("empty.csv", b"", "no header"),
])
def test_unusable_files_are_refused(authed, name, data, fragment):
    r = _upload(authed, name, data)
    assert r.status_code == 400 and fragment in r.json()["detail"]


def test_the_size_limit(authed, monkeypatch):
    from app import library_io
    monkeypatch.setattr(library_io, "MAX_IMPORT_BYTES", 50)
    assert _upload(authed, "big.csv", b"filename,tags\n" + b"x,y\n" * 100).status_code == 400


def test_an_import_is_recorded_and_viewers_cannot_import_but_can_export(authed, trio):
    tag, (a, b, c) = trio
    _upload(authed, "s.csv", f"filename,tags\n{a['filename']},recorded-import\n".encode())
    assert authed.get("/api/activity", params={"limit": 1}).json()["items"][0]["action"] == "import"
    authed.post("/api/users", json={"username": "ioviewer", "password": PW, "role": "viewer"})
    from app.main import app
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "ioviewer", "password": PW})
        assert viewer.get("/api/library-io/export.json").status_code == 200
        assert _upload(viewer, "s.csv", b"filename,tags\nx,y\n").status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert TestClient(app).get("/api/library-io/export.csv").status_code == 401
