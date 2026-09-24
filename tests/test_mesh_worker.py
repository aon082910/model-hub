"""Coverage for out-of-process, memory-bounded mesh analysis.

Background: trimesh needs many times a file's size to load it (measured 4.1GB
for a 35MB multi-part .3mf), and doing that inside the web server process let a
single large file get the whole app OOM-killed on every scan. See
app/mesh_worker.py.
"""
import base64
import json
import os
import time
import zipfile

import numpy as np
import pytest
import trimesh

from app import thumbnails
from app.mesh_worker import MeshWorkerError, MeshWorkerPool

MiB = 1024 * 1024

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def thumb_dir(monkeypatch, tmp_path):
    d = tmp_path / "thumbs"
    d.mkdir()
    monkeypatch.setattr(thumbnails, "THUMB_DIR", d)
    return d


@pytest.fixture
def no_mesh_loading(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("load_mesh must not be called on this path")
    monkeypatch.setattr(thumbnails, "load_mesh", _boom)


def _binary_stl(path, extents=(10.0, 20.0, 30.0)):
    mesh = trimesh.creation.box(extents=extents)
    mesh.export(str(path))
    return mesh


# ---------- no-parse previews ----------

def test_binary_stl_face_count_rejects_ascii(tmp_path):
    ascii_stl = tmp_path / "a.stl"
    ascii_stl.write_text("solid x\nendsolid x\n")
    binary = tmp_path / "b.stl"
    _binary_stl(binary)

    assert thumbnails.binary_stl_face_count(ascii_stl) is None
    assert thumbnails.binary_stl_face_count(binary) == 12


def test_binary_stl_preview_reads_exact_bounds_without_a_mesh(tmp_path, no_mesh_loading):
    path = tmp_path / "box.stl"
    mesh = _binary_stl(path)

    triangles, bounds, count = thumbnails.binary_stl_preview(path, max_faces=5, chunk_faces=4)

    assert count == 12
    assert triangles.shape == (5, 3, 3)
    np.testing.assert_allclose(bounds, mesh.bounds, atol=1e-5)


def test_estimate_parse_bytes_for_3mf_uses_uncompressed_model_size(tmp_path):
    path = tmp_path / "p.3mf"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "x" * 1000)
        zf.writestr("3D/Objects/object_1.model", "y" * 3000)
        zf.writestr("Metadata/thumbnail.png", _TINY_PNG)

    assert thumbnails.estimate_parse_bytes(path) == 4000 * thumbnails.PARSE_BYTES_PER_3MF_XML_BYTE


# ---------- analyze_mesh budget routing ----------

def test_over_budget_binary_stl_indexed_from_streamed_preview(tmp_path, thumb_dir, no_mesh_loading):
    path = tmp_path / "huge.stl"
    _binary_stl(path)

    result = thumbnails.analyze_mesh(path, budget_bytes=1)

    assert result["analysis"] == "preview-only"
    assert result["face_count"] == 12
    np.testing.assert_allclose(result["bbox"], (10.0, 20.0, 30.0), atol=1e-4)
    assert result["volume_mm3"] == pytest.approx(6000.0, rel=1e-4)
    assert (thumb_dir / result["thumbnail_path"]).read_bytes().startswith(b"\x89PNG")
    assert "mesh worker budget" in result["errors"][0]


def test_over_budget_3mf_uses_embedded_preview(tmp_path, thumb_dir, no_mesh_loading):
    path = tmp_path / "plate.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "x" * 10_000)
        zf.writestr("Metadata/thumbnail.png", _TINY_PNG)

    result = thumbnails.analyze_mesh(path, budget_bytes=1)

    assert result["analysis"] == "preview-only"
    assert (thumb_dir / result["thumbnail_path"]).read_bytes() == _TINY_PNG
    assert result["face_count"] is None


def test_within_budget_gets_full_analysis(tmp_path, thumb_dir):
    path = tmp_path / "box.stl"
    _binary_stl(path)

    result = thumbnails.analyze_mesh(path, budget_bytes=1024 * MiB)

    assert result["analysis"] == "full"
    assert result["face_count"] == 12
    assert result["is_watertight"] is True
    assert result["volume_mm3"] == pytest.approx(6000.0, rel=1e-4)
    assert result["errors"] == []


def test_failed_parse_falls_back_to_preview(monkeypatch, tmp_path, thumb_dir):
    path = tmp_path / "box.stl"
    _binary_stl(path)

    def _refuse(*a, **k):
        raise ValueError("Memory allocation failed")
    monkeypatch.setattr(thumbnails, "load_mesh", _refuse)

    result = thumbnails.analyze_mesh(path, budget_bytes=1024 * MiB)

    assert result["analysis"] == "preview-only"
    assert result["face_count"] == 12
    assert result["thumbnail_path"]
    assert "could not load mesh" in result["errors"][0]


# ---------- real worker processes ----------
# These run in spawned processes, so the helpers must be importable
# module-level functions.

def _try_allocate(mb):
    try:
        block = np.ones(mb * MiB // 8)
        return f"allocated {block.nbytes // MiB}"
    except MemoryError:
        return "refused"


def _die():
    os._exit(1)


def _nap(seconds):
    time.sleep(seconds)
    return "woke"


def test_worker_memory_ceiling_refuses_cleanly_and_worker_stays_usable():
    pool = MeshWorkerPool(workers=1, budget_bytes=512 * MiB)
    try:
        assert pool.run(_try_allocate, 2048, timeout=120) == "refused"
        assert pool.run(_try_allocate, 16, timeout=120) == "allocated 16"
    finally:
        pool.shutdown()


def test_dead_worker_is_reported_and_replaced():
    pool = MeshWorkerPool(workers=1, budget_bytes=512 * MiB)
    try:
        with pytest.raises(MeshWorkerError, match="died"):
            pool.run(_die, timeout=120)
        assert pool.run(_try_allocate, 1, timeout=120) == "allocated 1"
    finally:
        pool.shutdown()


def test_hung_worker_is_killed_and_replaced():
    pool = MeshWorkerPool(workers=1, budget_bytes=512 * MiB)
    try:
        pool.run(_try_allocate, 1, timeout=120)  # pay the spawn cost up front
        with pytest.raises(MeshWorkerError, match="did not finish"):
            pool.run(_nap, 60, timeout=2)
        started = time.monotonic()
        assert pool.run(_nap, 0, timeout=120) == "woke"
        # Replaced, not still stuck behind the 60s sleep.
        assert time.monotonic() - started < 30
    finally:
        pool.shutdown()


# ---------- crash-loop breaker ----------

class _FakePool:
    budget_bytes = 1024 * MiB

    def __init__(self):
        self.analyzed = []

    def run(self, fn, path_str, color, budget):
        self.analyzed.append(os.path.basename(path_str))
        return {
            "geometry_hash": None, "vertex_count": 8, "face_count": 12,
            "bbox": (1.0, 1.0, 1.0), "volume_mm3": 1.0, "is_watertight": True,
            "thumbnail_path": None, "analysis": "full", "errors": [],
        }


def _run_scan(monkeypatch, tmp_path, marker_contents):
    from sqlmodel import Session, select
    import app.scanner as scanner
    from app.db import engine
    from app.models import Model3D

    library = tmp_path / "library"
    library.mkdir()
    _binary_stl(library / "crashy-fixture.stl")
    _binary_stl(library / "innocent-fixture.stl", extents=(1, 2, 3))
    marker = tmp_path / "scan_inflight.json"
    if marker_contents is not None:
        marker.write_text(json.dumps(marker_contents))

    fake = _FakePool()
    monkeypatch.setattr(scanner, "LIBRARY_PATH", library)
    monkeypatch.setattr(scanner, "INFLIGHT_MARKER", marker)
    monkeypatch.setattr(scanner, "scan_worker_pool", lambda: fake)
    # The scan shares the suite's database; don't let it prune other tests' rows.
    monkeypatch.setattr(scanner, "_remove_missing_models", lambda session: None)

    with Session(engine) as session:
        scanner._scan_library(session)
        rows = session.exec(select(Model3D).where(Model3D.path.in_(
            ["crashy-fixture.stl", "innocent-fixture.stl"]))).all()
        indexed = sorted(r.path for r in rows)
        for r in rows:
            session.delete(r)
        session.commit()
    return fake.analyzed, indexed, marker.exists()


def test_file_that_crashed_two_scans_is_indexed_without_analysis(monkeypatch, tmp_path):
    analyzed, indexed, marker_left = _run_scan(
        monkeypatch, tmp_path, {"path": "crashy-fixture.stl", "strikes": 1}
    )

    assert analyzed == ["innocent-fixture.stl"]
    # Still indexed (so the next scan skips it as unchanged), just not analyzed.
    assert indexed == ["crashy-fixture.stl", "innocent-fixture.stl"]
    assert marker_left is False


def test_worker_death_falls_back_to_preview(monkeypatch, tmp_path):
    """If a full analysis kills its worker, the file still gets whatever the
    no-parse preview can give it (here, the .3mf's embedded thumbnail)."""
    from sqlmodel import Session
    import app.scanner as scanner
    from app.db import engine
    from app.mesh_worker import analyze_file, preview_file

    path = tmp_path / "died-fixture.3mf"
    path.write_bytes(b"placeholder")
    calls = []

    class _DyingPool:
        budget_bytes = 1024 * MiB

        def run(self, fn, *args):
            calls.append(fn)
            if fn is analyze_file:
                raise MeshWorkerError("mesh worker process died (most likely out of memory)")
            assert fn is preview_file
            return {**thumbnails._empty_analysis(), "thumbnail_path": "embedded.png"}

    monkeypatch.setattr(scanner, "scan_worker_pool", lambda: _DyingPool())

    with Session(engine) as session:
        model = scanner._upsert_path(session, path, "died-fixture.3mf", {})
        session.commit()
        assert model.thumbnail_path == "embedded.png"
        session.delete(model)
        session.commit()
    assert calls == [analyze_file, preview_file]


def test_single_interrupted_scan_does_not_skip_the_file(monkeypatch, tmp_path):
    # One strike can just be an ordinary restart mid-scan.
    analyzed, indexed, _ = _run_scan(
        monkeypatch, tmp_path, {"path": "crashy-fixture.stl", "strikes": 0}
    )

    assert sorted(analyzed) == ["crashy-fixture.stl", "innocent-fixture.stl"]
    assert indexed == ["crashy-fixture.stl", "innocent-fixture.stl"]
