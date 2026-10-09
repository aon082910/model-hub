"""Turn a textured 3D scene (GLB) into one printable STL: the parts are joined, moved to sit on the origin and scaled so the longest side is the size asked for.
Objaverse objects are modelled in arbitrary units (often a unit-sized object), so the size is a choice, not a measurement: the STL says nothing about the real thing."""
from pathlib import Path

DEFAULT_LONGEST_MM = 80.0
MAX_FACES = 3_000_000


def glb_to_stl(src: Path, dest: Path, longest_mm: float = DEFAULT_LONGEST_MM) -> dict:
    import numpy as np
    import trimesh
    mesh = trimesh.load(str(src), force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)
    if mesh is None or not hasattr(mesh, "faces") or len(mesh.faces) == 0:
        raise ValueError("That model has no surface to print (it may be points or lines only)")
    if len(mesh.faces) > MAX_FACES:
        raise ValueError(f"That model has {len(mesh.faces):,} faces, too many to convert")
    extents = np.asarray(mesh.extents, dtype=float)
    biggest = float(extents.max())
    if not np.isfinite(biggest) or biggest <= 0:
        raise ValueError("That model has no size")
    mesh.apply_scale(longest_mm / biggest)
    mesh.apply_translation(-mesh.bounds[0])
    mesh.export(str(dest), file_type="stl")
    return {"faces": int(len(mesh.faces)), "size_mm": [round(float(x), 1) for x in mesh.extents]}
