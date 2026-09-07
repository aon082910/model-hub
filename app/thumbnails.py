import hashlib
from pathlib import Path

import numpy as np
import trimesh

from app.config import THUMB_DIR

# Keep mesh analysis/rendering bounded for very detailed models. A 512px preview
# does not benefit from millions of rendered triangles, and constructing a convex
# hull for a huge non-watertight mesh can consume gigabytes of RAM.
GEOMETRY_HASH_CHUNK_VERTICES = 100_000
MAX_RENDER_FACES = 50_000
MAX_CONVEX_HULL_FACES = 50_000


def load_mesh(path: Path):
    """Load and validate one mesh for reuse by stats and thumbnail generation."""
    try:
        mesh = trimesh.load(str(path), force="mesh")
        if isinstance(mesh, trimesh.Scene):
            mesh = mesh.dump(concatenate=True)
    except Exception as exc:
        raise ValueError(
            f"invalid or unsupported {path.suffix.lower()} mesh: {exc}"
        ) from exc

    if mesh is None or not hasattr(mesh, "vertices") or not hasattr(mesh, "faces"):
        raise ValueError("mesh loader returned no mesh geometry")

    vertices = np.asarray(mesh.vertices)
    faces = np.asarray(mesh.faces)
    if vertices.ndim != 2 or len(vertices) == 0:
        raise ValueError("mesh contains no vertices")
    if faces.ndim != 2 or len(faces) == 0:
        raise ValueError("mesh contains no faces")

    return mesh


def _geometry_hash(vertices: np.ndarray) -> str:
    """Translation-independent geometry hash without copying the full mesh."""
    center = vertices.mean(axis=0)
    digest = hashlib.sha256()
    for start in range(0, len(vertices), GEOMETRY_HASH_CHUNK_VERTICES):
        chunk = vertices[start : start + GEOMETRY_HASH_CHUNK_VERTICES]
        rounded = np.round(chunk - center, 3)
        digest.update(rounded.tobytes())
    return digest.hexdigest()


def mesh_stats(path: Path, mesh=None) -> dict:
    if mesh is None:
        mesh = load_mesh(path)

    verts = np.asarray(mesh.vertices)
    faces = np.asarray(mesh.faces)
    if len(verts) == 0 or len(faces) == 0:
        raise ValueError("mesh contains no renderable geometry")

    geometry_hash = _geometry_hash(verts)
    bbox = np.asarray(mesh.bounding_box.extents, dtype=float).tolist()

    watertight = bool(getattr(mesh, "is_watertight", False))
    if watertight:
        volume_mm3 = abs(float(mesh.volume))
    elif len(faces) <= MAX_CONVEX_HULL_FACES:
        # Preserve the existing convex-hull approximation for modest meshes.
        try:
            volume_mm3 = abs(float(mesh.convex_hull.volume))
        except Exception:
            volume_mm3 = None
    else:
        # Convex hull generation can require enormous transient allocations on
        # detailed meshes. The axis-aligned bounding box is a coarser upper bound
        # but is effectively free because the extents are already known.
        volume_mm3 = float(np.prod(bbox)) if all(v > 0 for v in bbox) else None

    return {
        "geometry_hash": geometry_hash,
        "vertex_count": int(len(verts)),
        "face_count": int(len(faces)),
        "bbox": tuple(bbox),
        "volume_mm3": volume_mm3,
        "is_watertight": watertight,
    }


def generate_thumbnail(path: Path, size: int = 512, mesh=None) -> str:
    """Render an isometric snapshot of the mesh to a PNG in THUMB_DIR.
    Returns the thumbnail filename (relative to THUMB_DIR), or None on failure.

    Uses matplotlib exclusively (not trimesh's pyglet/GL scene renderer) --
    that renderer needs a working X/EGL context, which is unreliable in a
    minimal headless container (xvfb-run's readiness check can hang with no
    clear error). matplotlib needs no display server at all.
    """
    if mesh is None:
        mesh = load_mesh(path)
    png = _matplotlib_fallback(mesh, size)

    if png is None:
        return None

    out_name = hashlib.sha1(str(path).encode()).hexdigest() + ".png"
    out_path = THUMB_DIR / out_name
    with open(out_path, "wb") as f:
        f.write(png)
    return out_name


def _matplotlib_fallback(mesh, size: int) -> bytes:
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    vertices = np.asarray(mesh.vertices)
    face_indices = np.asarray(mesh.faces)
    if len(vertices) == 0 or len(face_indices) == 0:
        raise ValueError("mesh contains no renderable geometry")

    # Sample faces evenly across very detailed meshes before materializing the
    # (face, vertex, xyz) array used by Matplotlib. This bounds the largest copy.
    if len(face_indices) > MAX_RENDER_FACES:
        sample = np.linspace(
            0, len(face_indices) - 1, MAX_RENDER_FACES, dtype=np.int64
        )
        face_indices = face_indices[sample]
    triangles = vertices[face_indices]

    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
    try:
        ax = fig.add_subplot(projection="3d")
        collection = Poly3DCollection(
            triangles, facecolor="#4f8ef7", edgecolor="none", linewidths=0
        )
        ax.add_collection3d(collection)
        bounds = np.asarray(mesh.bounds)
        if bounds.shape != (2, 3):
            raise ValueError("mesh has invalid bounds")
        ax.set_xlim(bounds[0][0], bounds[1][0])
        ax.set_ylim(bounds[0][1], bounds[1][1])
        ax.set_zlim(bounds[0][2], bounds[1][2])
        ax.set_box_aspect((1, 1, 1))
        ax.axis("off")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", transparent=True)
        return buf.getvalue()
    finally:
        plt.close(fig)
