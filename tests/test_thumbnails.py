import base64
import zipfile

import numpy as np
import pytest
import trimesh

from app import thumbnails

# A real, minimal 1x1 PNG -- used wherever a test needs bytes that pass
# thumbnails.extract_3mf_thumbnail's magic-byte validation.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_load_mesh_rejects_empty_geometry(monkeypatch, tmp_path):
    path = tmp_path / "empty.stl"
    path.write_bytes(b"not really an stl")

    class EmptyMesh:
        vertices = np.empty((0, 3))
        faces = np.empty((0, 3), dtype=np.int64)

    monkeypatch.setattr(trimesh, "load", lambda *args, **kwargs: EmptyMesh())

    with pytest.raises(ValueError, match="no vertices"):
        thumbnails.load_mesh(path)


def test_thumbnail_samples_large_face_sets(monkeypatch):
    vertices = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    faces = np.tile(np.array([[0, 1, 2]], dtype=np.int64), (100, 1))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    monkeypatch.setattr(thumbnails, "MAX_RENDER_FACES", 10)

    png = thumbnails._matplotlib_fallback(mesh, 64)

    assert isinstance(png, bytes)
    assert png.startswith(b"\x89PNG")


def test_large_non_watertight_mesh_skips_convex_hull(monkeypatch, tmp_path):
    path = tmp_path / "large.stl"
    path.write_bytes(b"placeholder")

    vertices = np.array(
        [
            [0.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [0.0, 3.0, 0.0],
            [0.0, 0.0, 4.0],
        ]
    )
    # trimesh's bounding box only considers vertices actually referenced by a
    # face, not the raw vertex array -- so all four vertices (including the
    # z=4 one) need to appear in at least one face, or the bbox comes back
    # degenerate (zero z-extent) and volume_mm3 is correctly None instead of 24.
    base_faces = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)
    faces = np.tile(base_faces, (5, 1))
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    monkeypatch.setattr(thumbnails, "MAX_CONVEX_HULL_FACES", 10)

    stats = thumbnails.mesh_stats(path, mesh=mesh)

    assert stats["is_watertight"] is False
    assert stats["volume_mm3"] == pytest.approx(24.0)


def test_watertight_check_skipped_for_huge_meshes(monkeypatch, tmp_path):
    """A mesh past MAX_WATERTIGHT_CHECK_FACES must not have is_watertight
    computed at all, even if it actually is watertight -- multi-plate .3mf
    project files with several million concatenated faces have been observed
    to consume >15GB of RAM inside trimesh's own is_watertight computation,
    OOM-killing the container. This is the guard against that, so it must
    trigger purely on face count, before is_watertight is ever touched."""
    path = tmp_path / "huge.stl"
    path.write_bytes(b"placeholder")

    # A real, genuinely watertight cube.
    mesh = trimesh.creation.box(extents=(2, 2, 2))
    assert mesh.is_watertight  # sanity check on the fixture itself

    monkeypatch.setattr(thumbnails, "MAX_WATERTIGHT_CHECK_FACES", 1)
    monkeypatch.setattr(thumbnails, "MAX_CONVEX_HULL_FACES", 1000)  # stay above this cube's face count

    stats = thumbnails.mesh_stats(path, mesh=mesh)

    assert stats["is_watertight"] is False
    # Forced onto the convex-hull fallback instead of the (skipped) real
    # volume -- still a sane number for a 2x2x2 cube (true volume 8).
    assert stats["volume_mm3"] == pytest.approx(8.0, rel=0.05)


def _make_3mf_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_extract_3mf_thumbnail_prefers_conventional_path(tmp_path):
    path = tmp_path / "model.3mf"
    _make_3mf_zip(path, {
        "Metadata/thumbnail.png": _TINY_PNG,
        "Auxiliaries/.thumbnails/plate_1.png": b"not the preferred one",
    })

    result = thumbnails.extract_3mf_thumbnail(path)

    assert result == _TINY_PNG


def test_extract_3mf_thumbnail_falls_back_to_auxiliaries(tmp_path):
    path = tmp_path / "model.3mf"
    _make_3mf_zip(path, {
        "Auxiliaries/.thumbnails/plate_1.png": _TINY_PNG,
    })

    result = thumbnails.extract_3mf_thumbnail(path)

    assert result == _TINY_PNG


def test_extract_3mf_thumbnail_returns_none_without_one(tmp_path):
    path = tmp_path / "model.3mf"
    _make_3mf_zip(path, {
        "3D/3dmodel.model": b"<xml/>",
    })

    assert thumbnails.extract_3mf_thumbnail(path) is None


def test_extract_3mf_thumbnail_rejects_invalid_png_bytes(tmp_path):
    path = tmp_path / "model.3mf"
    _make_3mf_zip(path, {
        "Metadata/thumbnail.png": b"this is not actually a png",
    })

    assert thumbnails.extract_3mf_thumbnail(path) is None


def test_extract_3mf_thumbnail_handles_non_zip_file(tmp_path):
    path = tmp_path / "corrupt.3mf"
    path.write_bytes(b"not a zip file at all")

    assert thumbnails.extract_3mf_thumbnail(path) is None


def test_generate_thumbnail_uses_embedded_3mf_preview_without_loading_mesh(monkeypatch, tmp_path):
    """The whole point is to skip mesh parsing entirely for the thumbnail
    when a usable embedded preview already exists -- assert that by making
    load_mesh a hard failure and confirming it's never reached."""
    path = tmp_path / "model.3mf"
    _make_3mf_zip(path, {"Metadata/thumbnail.png": _TINY_PNG})

    def _boom(*a, **k):
        raise AssertionError("load_mesh should not be called when an embedded thumbnail exists")

    monkeypatch.setattr(thumbnails, "load_mesh", _boom)
    monkeypatch.setattr(thumbnails, "THUMB_DIR", tmp_path)

    out_name = thumbnails.generate_thumbnail(path)

    assert out_name is not None
    assert (tmp_path / out_name).read_bytes() == _TINY_PNG
