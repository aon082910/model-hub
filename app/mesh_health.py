"""Is a model's mesh sound enough to print, and a repaired copy when it is not.

A repair fixes what can be fixed without changing the shape: duplicate and degenerate faces, flipped or inconsistent faces, holes (filled flat)
and loose vertices. It never overwrites the original: the result is a new file, grouped with the original as its "repaired" version."""
from pathlib import Path

MAX_FACES = 2_000_000


def _load(path: Path):
    from app.thumbnails import load_mesh
    try:
        return load_mesh(path)                    # the same loader as the thumbnails, including the second way to read a 3MF
    except ValueError:
        raise ValueError("That file has no surface to check")


def _health(mesh) -> dict:
    import numpy as np
    faces = int(len(mesh.faces))
    out = {"faces": faces, "vertices": int(len(mesh.vertices)), "checked": faces <= MAX_FACES}
    if not out["checked"]:
        out.update(ok=None, issues=[f"The mesh has {faces:,} faces, too many to check here"], fixable=False, watertight=None)
        return out
    edges = mesh.edges_sorted
    _, counts = np.unique(edges, axis=0, return_counts=True)
    open_edges, non_manifold = int((counts == 1).sum()), int((counts > 2).sum())
    degenerate = int((mesh.area_faces < 1e-12).sum())
    rows = np.sort(mesh.faces, axis=1)
    duplicates = int(faces - len(np.unique(rows, axis=0)))
    winding = bool(mesh.is_winding_consistent)
    watertight = bool(mesh.is_watertight)
    inverted = bool(watertight and mesh.volume < 0)
    bodies = int(mesh.body_count)
    issues = []
    if open_edges:
        issues.append(f"{open_edges:,} open edge{'s' if open_edges != 1 else ''}: the surface has holes, so a slicer may print it wrongly or leave gaps")
    if non_manifold:
        issues.append(f"{non_manifold:,} edge{'s' if non_manifold != 1 else ''} shared by more than two faces (non-manifold)")
    if not winding:
        issues.append("Some faces point the wrong way (inconsistent winding)")
    if inverted:
        issues.append("The whole mesh is inside out (negative volume)")
    if degenerate:
        issues.append(f"{degenerate:,} zero-area face{'s' if degenerate != 1 else ''}")
    if duplicates:
        issues.append(f"{duplicates:,} duplicate face{'s' if duplicates != 1 else ''}")
    if bodies > 1:
        issues.append(f"{bodies} separate pieces (fine if that is intended)")
    out.update(watertight=watertight, winding_consistent=winding, inverted=inverted, open_edges=open_edges, non_manifold_edges=non_manifold,
               degenerate_faces=degenerate, duplicate_faces=duplicates, bodies=bodies, issues=issues,
               ok=not [i for i in issues if "separate pieces" not in i],
               fixable=bool(open_edges or not winding or inverted or degenerate or duplicates))
    return out


def health_of_file(path: Path, budget_bytes: int = 0) -> dict:
    return _health(_load(path))


def repair_to_file(src: Path, dest: Path, budget_bytes: int = 0) -> dict:
    import trimesh
    mesh = _load(src)
    before = _health(mesh)
    mesh.process(validate=True)                                  # merges vertices, drops duplicate and degenerate faces
    mesh.remove_unreferenced_vertices()
    trimesh.repair.fix_inversion(mesh)
    trimesh.repair.fix_winding(mesh)
    trimesh.repair.fix_normals(mesh)
    trimesh.repair.fill_holes(mesh)
    after = _health(mesh)
    mesh.export(str(dest), file_type="stl")
    return {"before": before, "after": after}
