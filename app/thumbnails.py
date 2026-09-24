import hashlib
import zipfile
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
# Building trimesh's internal edge/adjacency tables for is_watertight also
# scales with face count. Multi-plate/multi-object .3mf project files (e.g.
# exported by Bambu Studio/OrcaSlicer, which get concatenated into one mesh
# on load) can have several million faces once combined -- computing
# is_watertight on a mesh that size has been observed in the wild to consume
# well over 15GB of RAM, OOM-killing the container on every scan attempt
# since the scan just retries the same file after every restart. Treat any
# mesh past this size as non-watertight without even trying, the same way
# MAX_CONVEX_HULL_FACES already treats its own fallback as too expensive
# past a size. Set higher than the hull/render caps since is_watertight
# alone is cheaper than those, but still bounded.
MAX_WATERTIGHT_CHECK_FACES = 200_000
DEFAULT_THUMBNAIL_COLOR = "#c9ced6"
THUMBNAIL_RENDER_VERSION = 2


def normalize_thumbnail_color(value: str) -> str:
    """Return a safe six-digit hex color for Matplotlib thumbnail rendering."""
    if isinstance(value, str) and len(value) == 7 and value.startswith("#"):
        try:
            int(value[1:], 16)
            return value.lower()
        except ValueError:
            pass
    return DEFAULT_THUMBNAIL_COLOR


def thumbnail_filename(
    path: Path,
    size: int = 512,
    color: str = DEFAULT_THUMBNAIL_COLOR,
) -> str:
    """Return the deterministic cache filename for the current render settings.

    Including the color and render-version settings in the filename makes browser
    cache invalidation automatic and lets bulk regeneration skip thumbnails that
    are already current after an interrupted or repeated job.
    """
    signature = "\0".join(
        (
            str(path),
            str(size),
            normalize_thumbnail_color(color),
            str(MAX_RENDER_FACES),
            str(THUMBNAIL_RENDER_VERSION),
        )
    )
    return hashlib.sha1(signature.encode()).hexdigest() + ".png"


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

    if len(faces) > MAX_WATERTIGHT_CHECK_FACES:
        watertight = False
    else:
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


def extract_3mf_thumbnail(path: Path) -> bytes:
    """Return the embedded preview PNG from a .3mf's OPC (zip) package, if any.

    Slicers (Bambu Studio, OrcaSlicer, PrusaSlicer, ...) already render and
    embed a plate preview inside the .3mf itself, conventionally at
    Metadata/thumbnail.png or under Auxiliaries/.thumbnails/. Using it
    directly skips mesh parsing entirely for the thumbnail -- which matters
    most for exactly the large multi-plate project files that are otherwise
    the most expensive (and were, before the is_watertight guard above, the
    most crash-prone) to render ourselves -- and gives a more accurate
    preview than our own flat-color render besides.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            candidates = [
                n for n in zf.namelist()
                if n.lower().endswith(".png")
                and ("thumbnail" in n.lower() or ".thumbnails/" in n.lower())
            ]
            if not candidates:
                return None
            preferred = next((n for n in candidates if n.lower() == "metadata/thumbnail.png"), None)
            # No conventional top-level thumbnail -- take the largest PNG among
            # the candidates, more likely to be a full preview than an icon.
            name = preferred or max(candidates, key=lambda n: zf.getinfo(n).file_size)
            data = zf.read(name)
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                return None
            return data
    except Exception:
        return None


def generate_thumbnail(
    path: Path,
    size: int = 512,
    mesh=None,
    color: str = DEFAULT_THUMBNAIL_COLOR,
) -> str:
    """Render an isometric snapshot of the mesh to a PNG in THUMB_DIR.
    Returns the thumbnail filename (relative to THUMB_DIR), or None on failure.

    Uses matplotlib exclusively (not trimesh's pyglet/GL scene renderer) --
    that renderer needs a working X/EGL context, which is unreliable in a
    minimal headless container (xvfb-run's readiness check can hang with no
    clear error). matplotlib needs no display server at all.
    """
    color = normalize_thumbnail_color(color)

    png = None
    if path.suffix.lower() == ".3mf":
        png = extract_3mf_thumbnail(path)

    if png is None:
        if mesh is None:
            mesh = load_mesh(path)
        png = _matplotlib_fallback(mesh, size, color)

    if png is None:
        return None

    out_name = thumbnail_filename(path, size=size, color=color)
    out_path = THUMB_DIR / out_name
    with open(out_path, "wb") as f:
        f.write(png)
    return out_name


def render_thumbnail_file(
    path_str: str,
    color: str,
    size: int = 512,
) -> str:
    """Process-pool entry point for rendering one file without database access."""
    path = Path(path_str)
    mesh = None
    try:
        mesh = load_mesh(path)
        return generate_thumbnail(path, size=size, mesh=mesh, color=color)
    finally:
        if mesh is not None:
            del mesh


def _matplotlib_fallback(
    mesh,
    size: int,
    color: str = DEFAULT_THUMBNAIL_COLOR,
) -> bytes:
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
            triangles, facecolor=color, edgecolor="none", linewidths=0
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
