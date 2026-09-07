import numpy as np
import pytest
import trimesh

from app import thumbnails


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
