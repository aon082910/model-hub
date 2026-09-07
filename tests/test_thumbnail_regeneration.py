from pathlib import Path

import pytest


def test_generate_thumbnail_uses_configured_color(monkeypatch, tmp_path):
    import app.thumbnails as thumbnails

    captured = {}

    def fake_render(mesh, size, color):
        captured["mesh"] = mesh
        captured["size"] = size
        captured["color"] = color
        return b"png-bytes"

    mesh = object()
    model_path = tmp_path / "model.stl"
    model_path.write_bytes(b"mesh")
    thumb_dir = tmp_path / "thumbs"
    thumb_dir.mkdir()

    monkeypatch.setattr(thumbnails, "THUMB_DIR", thumb_dir)
    monkeypatch.setattr(thumbnails, "_matplotlib_fallback", fake_render)

    filename = thumbnails.generate_thumbnail(
        model_path,
        size=256,
        mesh=mesh,
        color="#123abc",
    )

    assert captured == {"mesh": mesh, "size": 256, "color": "#123abc"}
    assert (thumb_dir / filename).read_bytes() == b"png-bytes"


def test_thumbnail_job_only_regenerates_existing_thumbnail(monkeypatch, tmp_path):
    import app.library_maintenance as maintenance
    import app.thumbnail_jobs as jobs

    library = tmp_path / "library"
    library.mkdir()
    model_path = library / "model.stl"
    model_path.write_bytes(b"mesh")

    pages = [[(42, "model.stl")], []]
    persisted = []
    rendered = []

    monkeypatch.setattr(jobs, "LIBRARY_PATH", library)
    monkeypatch.setattr(jobs, "_load_job_configuration", lambda: ("#654321", 1))
    monkeypatch.setattr(jobs, "_load_model_page", lambda last_id: pages.pop(0))
    monkeypatch.setattr(jobs, "load_mesh", lambda path: object())

    def fake_generate(path, mesh=None, color=None):
        rendered.append((Path(path), color))
        return "model.png"

    monkeypatch.setattr(jobs, "generate_thumbnail", fake_generate)
    monkeypatch.setattr(
        jobs,
        "_persist_thumbnail",
        lambda model_id, thumbnail_path: persisted.append((model_id, thumbnail_path)),
    )
    monkeypatch.setattr(jobs.gc, "collect", lambda: 0)

    assert maintenance.acquire_library_maintenance("thumbnails") is True
    jobs._set_state(running=True, done=0, total=0, regenerated=0, failed=0)
    jobs._run_thumbnail_regeneration()

    assert rendered == [(model_path, "#654321")]
    assert persisted == [(42, "model.png")]
    assert jobs.thumbnail_regeneration_status() == {
        "running": False,
        "done": 1,
        "total": 1,
        "regenerated": 1,
        "failed": 0,
    }
    assert maintenance.current_library_maintenance() is None


def test_thumbnail_job_rejected_during_scan():
    import app.library_maintenance as maintenance
    import app.thumbnail_jobs as jobs

    assert maintenance.acquire_library_maintenance("scan") is True
    try:
        with pytest.raises(maintenance.LibraryMaintenanceBusy, match="scan"):
            jobs.start_thumbnail_regeneration()
    finally:
        maintenance.release_library_maintenance()
