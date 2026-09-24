from pathlib import Path

import pytest


def test_generate_thumbnail_uses_configured_color_and_signature(monkeypatch, tmp_path):
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
        color="#123ABC",
    )

    assert captured == {"mesh": mesh, "size": 256, "color": "#123abc"}
    assert filename == thumbnails.thumbnail_filename(
        model_path,
        size=256,
        color="#123abc",
    )
    assert (thumb_dir / filename).read_bytes() == b"png-bytes"
    assert filename != thumbnails.thumbnail_filename(
        model_path,
        size=256,
        color="#654321",
    )


def test_thumbnail_current_requires_matching_signature_and_file(monkeypatch, tmp_path):
    import app.thumbnail_jobs as jobs

    library = tmp_path / "library"
    library.mkdir()
    model_path = library / "model.stl"
    model_path.write_bytes(b"mesh")
    thumb_dir = tmp_path / "thumbs"
    thumb_dir.mkdir()

    monkeypatch.setattr(jobs, "THUMB_DIR", thumb_dir)

    expected = jobs.thumbnail_filename(model_path, color="#123abc")
    assert jobs._thumbnail_is_current(model_path, expected, "#123abc") is False

    (thumb_dir / expected).write_bytes(b"png")
    assert jobs._thumbnail_is_current(model_path, expected, "#123abc") is True
    assert jobs._thumbnail_is_current(model_path, expected, "#654321") is False
    assert jobs._thumbnail_is_current(model_path, "legacy.png", "#123abc") is False


def test_render_worker_only_renders_thumbnail(monkeypatch, tmp_path):
    import app.mesh_worker as mesh_worker
    import app.thumbnails as thumbnails

    model_path = tmp_path / "model.stl"
    model_path.write_bytes(b"mesh")
    calls = []

    def fake_render(path, color, budget_bytes):
        calls.append((path, color, budget_bytes))
        return "signed-thumbnail.png"

    monkeypatch.setattr(thumbnails, "render_thumbnail_only", fake_render)

    result = mesh_worker.render_thumbnail(str(model_path), "#654321", 123)

    assert result == "signed-thumbnail.png"
    assert calls == [(model_path, "#654321", 123)]


def test_render_worker_rejects_missing_file(tmp_path):
    import app.mesh_worker as mesh_worker

    with pytest.raises(FileNotFoundError):
        mesh_worker.render_thumbnail(str(tmp_path / "gone.stl"), "#654321", 123)


def test_thumbnail_job_rejected_during_scan():
    import app.library_maintenance as maintenance
    import app.thumbnail_jobs as jobs

    assert maintenance.acquire_library_maintenance("scan") is True
    try:
        with pytest.raises(maintenance.LibraryMaintenanceBusy, match="scan"):
            jobs.start_thumbnail_regeneration()
    finally:
        maintenance.release_library_maintenance()
