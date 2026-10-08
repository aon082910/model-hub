"""Only model files are indexed or imported; zips are opened and just their models kept."""
import io
import os
import zipfile

from sqlmodel import Session, select

from app.db import engine
from app.models import Model3D, ModelTagLink, ProjectModelLink, Tag


def _stl(n=5):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


def _import(c, filename, content, **form):
    return c.post("/api/library/import", files={"file": (filename, content, "application/octet-stream")}, data=form)


def test_non_model_file_is_refused(authed):
    for name in ("notes.txt", "drawing.svg", "sketch.3dm", "pack.rar", "pack.7z", "noextension"):
        r = _import(authed, name, b"hello")
        assert r.status_code == 400, name
        assert "Only model files" in r.json()["detail"], name
    ok = _import(authed, "limits_ok.stl", _stl(6))
    assert ok.status_code == 200 and ok.json()["filename"] == "limits_ok.stl"


def test_import_never_uses_a_path_from_the_filename(authed, library_path):
    r = _import(authed, "../../escape_attempt.stl", _stl(7))
    assert r.status_code == 200, r.text
    assert r.json()["path"].replace("\\", "/").startswith("imported/")
    assert not os.path.exists(os.path.join(library_path, "..", "escape_attempt.stl"))


def test_zip_imports_only_the_models_inside(authed, library_path):
    archive = _zip({
        "Cool Part/body.stl": _stl(8),
        "Cool Part/lid.3mf": b"not really a 3mf but named like one",
        "Cool Part/README.txt": b"print me",
        "Cool Part/photo.jpg": b"jpegbytes",
        "Cool Part/sliced.gcode": b"G1 X0",
        "Cool Part/inner.zip": _zip({"deep.stl": _stl(3)}),
        "__MACOSX/Cool Part/._body.stl": b"junk",
        "../../traversal.stl": _stl(9),
    })
    r = _import(authed, "Cool Part.zip", archive, source_url="https://example.com/not-a-supported-site")
    assert r.status_code == 200, r.text
    data = r.json()
    names = sorted(m["filename"] for m in data["models"])
    assert names == ["body.stl", "lid.3mf", "traversal.stl"]          # flattened to plain file names
    assert data["imported"] == 3 and data["ignored"] == 5
    for m in data["models"]:
        assert m["path"].replace("\\", "/").startswith("imported/Cool Part/")
        assert m["source_url"] == "https://example.com/not-a-supported-site"
    on_disk = sorted(os.listdir(os.path.join(library_path, "imported", "Cool Part")))
    assert on_disk == ["body.stl", "lid.3mf", "traversal.stl"]        # nothing else was written
    assert not os.path.exists(os.path.join(library_path, "traversal.stl"))


def test_zip_problems_are_reported(authed):
    assert _import(authed, "empty.zip", _zip({"readme.txt": b"x"})).status_code == 400
    assert "No model files" in _import(authed, "empty2.zip", _zip({"a.png": b"x"})).json()["detail"]
    assert "could not be read" in _import(authed, "broken.zip", b"this is not a zip").json()["detail"]


def test_scan_ignores_non_model_files(authed, library_path):
    folder = os.path.join(library_path, "scan_limits")
    os.makedirs(folder, exist_ok=True)
    for name, data in (("scan_keep.stl", _stl(11)), ("notes.txt", b"hi"), ("pack.zip", _zip({"x.stl": _stl()})),
                       ("drawing.svg", b"<svg/>"), ("model.3dm", b"x")):
        with open(os.path.join(folder, name), "wb") as f:
            f.write(data)
    assert authed.post("/api/library/scan").status_code == 200
    with Session(engine) as session:
        indexed = {m.filename for m in session.exec(select(Model3D).where(Model3D.path.like("scan_limits%"))).all()}
    assert indexed == {"scan_keep.stl"}


def _legacy_row(path, ext):
    with Session(engine) as session:
        row = Model3D(filename=os.path.basename(path), path=path, extension=ext, size_bytes=1, content_hash="legacy-" + path)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_legacy_non_model_rows_are_hidden_and_removable(authed):
    zip_id = _legacy_row("legacy/old_pack.zip", ".zip")
    txt_id = _legacy_row("legacy/old_notes.txt", ".txt")
    keep = _import(authed, "legacy_keep.stl", _stl(12)).json()

    listed = {m["id"] for m in authed.get("/api/library/models?limit=5000").json()}
    assert zip_id not in listed and txt_id not in listed and keep["id"] in listed

    # give the zip row some links that must not be left dangling
    pid = authed.post("/api/projects", json={"name": "Legacy link project"}).json()["id"]
    with Session(engine) as session:
        tag = Tag(name="legacy-tag-x")
        session.add(tag)
        session.commit()
        session.refresh(tag)
        session.add(ModelTagLink(model_id=zip_id, tag_id=tag.id))
        session.add(ProjectModelLink(project_id=pid, model_id=zip_id))
        session.commit()

    summary = authed.get("/api/library/non-model-files").json()
    assert summary["count"] >= 2 and ".zip" in summary["by_extension"] and ".txt" in summary["by_extension"]
    assert ".stl" in summary["model_types"] and ".zip" not in summary["model_types"]

    removed = authed.post("/api/library/non-model-files/remove").json()["removed"]
    assert removed >= 2
    assert authed.get("/api/library/non-model-files").json()["count"] == 0
    with Session(engine) as session:
        assert session.get(Model3D, zip_id) is None
        assert session.exec(select(ModelTagLink).where(ModelTagLink.model_id == zip_id)).all() == []
        assert session.exec(select(ProjectModelLink).where(ProjectModelLink.model_id == zip_id)).all() == []
    assert authed.get(f"/api/library/models/{keep['id']}").status_code == 200       # real models untouched


def test_deleting_a_model_leaves_no_dangling_links(authed):
    model = _import(authed, "delete_links.stl", _stl(13)).json()
    pid = authed.post("/api/projects", json={"name": "Delete links project"}).json()["id"]
    authed.post(f"/api/projects/{pid}/models/{model['id']}")
    authed.post(f"/api/tags/models/{model['id']}", json={"name": "delete-links-tag"})
    qid = authed.post("/api/queue", json={"model_id": model["id"]}).json()["id"]

    assert authed.delete(f"/api/library/models/{model['id']}").status_code == 200
    with Session(engine) as session:
        assert session.exec(select(ProjectModelLink).where(ProjectModelLink.model_id == model["id"])).all() == []
        assert session.exec(select(ModelTagLink).where(ModelTagLink.model_id == model["id"])).all() == []
    assert qid not in [q["id"] for q in authed.get("/api/queue").json()]
    assert authed.get(f"/api/projects/{pid}").json()["models"] == []


def test_duplicate_flags_are_cleared_when_the_original_is_removed(authed):
    original = _import(authed, "dup_original.stl", _stl(14)).json()
    with Session(engine) as session:
        copy = Model3D(filename="dup_copy.stl", path="dups/dup_copy.stl", extension=".stl", size_bytes=1,
                       content_hash="dup-copy-hash", is_duplicate_of=original["id"])
        session.add(copy)
        session.commit()
        session.refresh(copy)
        copy_id = copy.id
    assert authed.delete(f"/api/library/models/{original['id']}").status_code == 200
    assert authed.get(f"/api/library/models/{copy_id}").json()["is_duplicate_of"] is None
