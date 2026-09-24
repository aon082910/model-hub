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
# scales with face count, and multi-plate .3mf project files (Bambu Studio,
# OrcaSlicer) get concatenated into one mesh of several million faces on load.
# Treat any mesh past this size as non-watertight without trying, the same way
# MAX_CONVEX_HULL_FACES bounds its own fallback.
MAX_WATERTIGHT_CHECK_FACES = 200_000
DEFAULT_THUMBNAIL_COLOR = "#c9ced6"
THUMBNAIL_RENDER_VERSION = 2

# Peak memory to *load* a file with trimesh, per byte of input, measured in
# the container: a 35MB Bambu-style .3mf holding 207MB of uncompressed model
# XML peaked at 4.1GB (~20x the XML), and a 262MB binary STL at 2.9GB (~11x).
# Used to decide up front whether a file fits the mesh worker's memory budget
# (see app/mesh_worker.py) before spending minutes parsing it only to hit the
# ceiling. Rounded up: underestimating just means the worker's hard limit
# catches it instead.
PARSE_BYTES_PER_3MF_XML_BYTE = 20
PARSE_BYTES_PER_BINARY_STL_BYTE = 12
# Largest embedded preview we'll read out of a .3mf -- real slicer previews are
# tens to hundreds of KB; this only guards against a pathological archive.
MAX_EMBEDDED_THUMBNAIL_BYTES = 20 * 1024 * 1024

# Binary STL: 80-byte header, uint32 face count, then 50 bytes per face.
_STL_RECORD = np.dtype([("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


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
    Metadata/thumbnail.png or under Auxiliaries/.thumbnails/. Using it skips
    mesh parsing for the thumbnail entirely -- which matters most for exactly
    the large multi-plate project files that are otherwise the most expensive
    to parse -- and is a more accurate preview than our own flat-color render.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            candidates = [
                info for info in zf.infolist()
                if info.filename.lower().endswith(".png")
                and ("thumbnail" in info.filename.lower() or ".thumbnails/" in info.filename.lower())
                and info.file_size <= MAX_EMBEDDED_THUMBNAIL_BYTES
            ]
            if not candidates:
                return None
            preferred = next(
                (i for i in candidates if i.filename.lower() == "metadata/thumbnail.png"), None
            )
            # No conventional top-level thumbnail -- take the largest PNG among
            # the candidates, more likely to be a full preview than an icon.
            info = preferred or max(candidates, key=lambda i: i.file_size)
            data = zf.read(info)
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                return None
            return data
    except Exception:
        return None


def binary_stl_face_count(path: Path):
    """Face count from a binary STL's header, or None if it isn't one.

    Validated against the file size (84 + 50 bytes per face), which is the
    only reliable test -- plenty of binary STLs start their header with the
    word "solid" just like ASCII ones do.
    """
    try:
        size = path.stat().st_size
        if size < 84:
            return None
        with open(path, "rb") as f:
            f.seek(80)
            count = int.from_bytes(f.read(4), "little")
        if count > 0 and size == 84 + 50 * count:
            return count
    except OSError:
        pass
    return None


def binary_stl_preview(path: Path, max_faces: int = MAX_RENDER_FACES, chunk_faces: int = 1_000_000):
    """(triangles, bounds, face_count) for a binary STL, read straight from disk.

    Never builds a mesh: exact bounds come from one chunked pass over the file
    and the triangles from an evenly spaced sample of max_faces records, so
    memory stays bounded by the chunk and sample sizes no matter how large the
    file is. Returns None for anything that isn't a valid binary STL.
    """
    count = binary_stl_face_count(path)
    if count is None:
        return None
    records = np.memmap(path, dtype=_STL_RECORD, mode="r", offset=84, shape=(count,))
    try:
        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)
        for start in range(0, count, chunk_faces):
            vertices = np.asarray(records["v"][start:start + chunk_faces], dtype=np.float64).reshape(-1, 3)
            lo = np.minimum(lo, vertices.min(axis=0))
            hi = np.maximum(hi, vertices.max(axis=0))
        sample = np.linspace(0, count - 1, min(count, max_faces), dtype=np.int64)
        triangles = np.asarray(records["v"][sample], dtype=np.float64)
    finally:
        del records
    if not (np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
        return None
    return triangles, np.array([lo, hi]), count


def estimate_parse_bytes(path: Path):
    """Rough peak memory trimesh would need to load this file, or None if unknown.

    Only estimated for the formats where it's cheap and where the big files
    actually show up: .3mf (from the zip directory's uncompressed sizes, no
    decompression) and binary STL (from the file size). Everything else is
    left to the mesh worker's hard memory limit.
    """
    ext = path.suffix.lower()
    try:
        if ext == ".3mf":
            with zipfile.ZipFile(path) as zf:
                xml_bytes = sum(
                    info.file_size for info in zf.infolist()
                    if info.filename.lower().endswith(".model")
                )
            return xml_bytes * PARSE_BYTES_PER_3MF_XML_BYTE
        if ext == ".stl" and binary_stl_face_count(path) is not None:
            return path.stat().st_size * PARSE_BYTES_PER_BINARY_STL_BYTE
    except Exception:
        return None
    return None


def _write_thumbnail(path: Path, png: bytes, size: int, color: str) -> str:
    out_name = thumbnail_filename(path, size=size, color=color)
    with open(THUMB_DIR / out_name, "wb") as f:
        f.write(png)
    return out_name


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
    return _write_thumbnail(path, png, size, color)


def _empty_analysis() -> dict:
    return {
        "geometry_hash": None,
        "vertex_count": None,
        "face_count": None,
        "bbox": (None, None, None),
        "volume_mm3": None,
        "is_watertight": None,
        "thumbnail_path": None,
        "analysis": "preview-only",
        "errors": [],
    }


def preview_without_parsing(path: Path, color: str = DEFAULT_THUMBNAIL_COLOR, size: int = 512) -> dict:
    """Whatever can be learned about a mesh file without loading it as a mesh.

    For files too large to parse within the memory budget (or that failed to
    parse at all): a .3mf's own embedded slicer preview, or for a binary STL a
    sampled render plus exact face count and bounding box read straight off
    disk. Returns the same keys as analyze_mesh; unknown values are None.
    """
    color = normalize_thumbnail_color(color)
    result = _empty_analysis()
    ext = path.suffix.lower()
    if ext == ".3mf":
        png = extract_3mf_thumbnail(path)
        if png is not None:
            result["thumbnail_path"] = _write_thumbnail(path, png, size, color)
    elif ext == ".stl":
        preview = binary_stl_preview(path)
        if preview is not None:
            triangles, bounds, count = preview
            extents = (bounds[1] - bounds[0]).tolist()
            result["face_count"] = int(count)
            result["bbox"] = tuple(extents)
            # Same coarse bounding-box volume the full path falls back to for
            # meshes past MAX_CONVEX_HULL_FACES.
            result["volume_mm3"] = float(np.prod(extents)) if all(e > 0 for e in extents) else None
            result["thumbnail_path"] = _write_thumbnail(
                path, _render_triangles(triangles, bounds, size, color), size, color
            )
    return result


def analyze_mesh(path: Path, color: str = DEFAULT_THUMBNAIL_COLOR, budget_bytes: int = None) -> dict:
    """Stats and thumbnail for one mesh file, bounded by budget_bytes.

    Meant to run inside the memory-capped mesh worker (app/mesh_worker.py),
    never in the web server process. Files estimated to need more than the
    budget skip the full parse and get preview_without_parsing() instead; a
    full parse that fails anyway (hits the worker's hard limit, or the file is
    just malformed) falls back to the same. Per-step problems are returned in
    "errors" for the caller to log, since a spawned worker has no logging
    configuration of its own.
    """
    estimate = estimate_parse_bytes(path)
    if budget_bytes is not None and estimate is not None and estimate > budget_bytes:
        result = preview_without_parsing(path, color)
        result["errors"].append(
            f"needs an estimated {estimate // 2**20} MB to load, over the {budget_bytes // 2**20} MB "
            "mesh worker budget -- indexed from a preview only"
        )
        return result

    try:
        mesh = load_mesh(path)
    except Exception as exc:
        result = preview_without_parsing(path, color)
        result["errors"].append(f"could not load mesh ({exc}) -- indexed from a preview only")
        return result

    result = _empty_analysis()
    result["analysis"] = "full"
    try:
        try:
            result.update(mesh_stats(path, mesh=mesh))
        except Exception as exc:
            result["errors"].append(f"failed to read mesh stats: {exc}")
        try:
            result["thumbnail_path"] = generate_thumbnail(path, mesh=mesh, color=color)
        except Exception as exc:
            result["errors"].append(f"thumbnail generation failed: {exc}")
    finally:
        del mesh
    return result


def render_thumbnail_only(path: Path, color: str = DEFAULT_THUMBNAIL_COLOR, budget_bytes: int = None) -> str:
    """Thumbnail for an already-indexed file, parsing it only when it has to.

    Tries the no-parse preview first (embedded .3mf preview, streamed binary
    STL); only loads the mesh when that yields nothing and the file fits the
    budget. Returns the thumbnail filename, or None.
    """
    color = normalize_thumbnail_color(color)
    thumbnail = preview_without_parsing(path, color)["thumbnail_path"]
    if thumbnail:
        return thumbnail
    estimate = estimate_parse_bytes(path)
    if budget_bytes is not None and estimate is not None and estimate > budget_bytes:
        return None
    return generate_thumbnail(path, color=color)


def _matplotlib_fallback(
    mesh,
    size: int,
    color: str = DEFAULT_THUMBNAIL_COLOR,
) -> bytes:
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
    return _render_triangles(vertices[face_indices], np.asarray(mesh.bounds), size, color)


def _render_triangles(triangles, bounds, size: int, color: str = DEFAULT_THUMBNAIL_COLOR) -> bytes:
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    bounds = np.asarray(bounds)
    if bounds.shape != (2, 3):
        raise ValueError("mesh has invalid bounds")

    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
    try:
        ax = fig.add_subplot(projection="3d")
        collection = Poly3DCollection(
            triangles, facecolor=color, edgecolor="none", linewidths=0
        )
        ax.add_collection3d(collection)
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
