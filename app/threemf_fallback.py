"""A second way to read a 3MF when the general loader cannot: straight from its XML.

Slicer "project" files (Bambu Studio, OrcaSlicer, PrusaSlicer) keep their parts in separate model files that the main model refers to (the 3MF Production
extension), nest components inside components, and carry extra plates and settings. This reads every mesh object a build item reaches (or, if the file has no build list, every
mesh object in it), applies each component's transform, and joins them into one mesh. It reads the XML with entity expansion off and stops at a size limit."""
import re
import zipfile
from pathlib import Path
from typing import Optional

import numpy as np
from lxml import etree

MAX_XML_BYTES = 400 * 1024 * 1024
MAX_FACES = 6_000_000
MAX_DEPTH = 8
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)


class ThreeMFError(ValueError):
    pass


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _matrix(text: Optional[str]) -> np.ndarray:
    """A 3MF transform ("m00 m01 m02 m10 ... m32": three rows of the matrix, then the move) as a 4x4 matrix for column vectors."""
    out = np.eye(4)
    if not text:
        return out
    try:
        v = [float(x) for x in re.split(r"[\s,]+", text.strip()) if x]
    except ValueError:
        return out
    if len(v) != 12:
        return out
    out[:3, :3] = np.array(v[:9]).reshape(3, 3).T
    out[:3, 3] = v[9:]
    return out


def _parse_model(data: bytes) -> dict:
    """{"objects": {object id: {"mesh": (vertices, faces) or None, "components": [(object id, path or None, matrix)]}}, "build": [(object id, matrix)]}."""
    root = etree.fromstring(data, parser=PARSER)
    objects, build = {}, []
    for element in root.iter():
        name = _local(element.tag)
        if name == "object":
            oid = element.get("id")
            entry = {"mesh": None, "components": []}
            for child in element:
                cname = _local(child.tag)
                if cname == "mesh":
                    vertices, triangles = [], []
                    for part in child:
                        if _local(part.tag) == "vertices":
                            vertices = [(float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))) for v in part if _local(v.tag) == "vertex"]
                        elif _local(part.tag) == "triangles":
                            triangles = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))) for t in part if _local(t.tag) == "triangle"]
                    if vertices and triangles:
                        entry["mesh"] = (np.array(vertices, dtype=float), np.array(triangles, dtype=np.int64))
                elif cname == "components":
                    for comp in child:
                        if _local(comp.tag) == "component":
                            path = next((v for k, v in comp.attrib.items() if _local(k) == "path"), None)
                            entry["components"].append((comp.get("objectid"), path, _matrix(comp.get("transform"))))
            if oid is not None:
                objects[oid] = entry
        elif name == "item":
            build.append((element.get("objectid"), _matrix(element.get("transform"))))
    return {"objects": objects, "build": build}


def load(path: Path):
    """The 3MF as one trimesh.Trimesh. Raises ThreeMFError when it holds no mesh."""
    import trimesh
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ThreeMFError(f"not a readable 3MF archive ({e.__class__.__name__})")
    with z:
        names = {n.lower().lstrip("/"): n for n in z.namelist()}
        models = {}
        for low, real in names.items():
            if low.endswith(".model"):
                if z.getinfo(real).file_size > MAX_XML_BYTES:
                    raise ThreeMFError("a model file inside is too large to read")
                try:
                    models[low] = _parse_model(z.read(real))
                except (etree.XMLSyntaxError, ValueError, TypeError) as e:
                    raise ThreeMFError(f"a model file inside is damaged ({e.__class__.__name__})")
        main_name = "3d/3dmodel.model" if "3d/3dmodel.model" in models else next(iter(models), None)
        if not main_name:
            raise ThreeMFError("no model inside")
        parts, total = [], 0

        def walk(model_key: str, oid: str, matrix: np.ndarray, depth: int):
            nonlocal total
            if depth > MAX_DEPTH:
                return
            entry = (models.get(model_key) or {}).get("objects", {}).get(oid)
            if not entry:
                return
            if entry["mesh"] is not None:
                v, f = entry["mesh"]
                if f.max(initial=-1) >= len(v) or f.min(initial=0) < 0:
                    return
                total += len(f)
                if total > MAX_FACES:
                    raise ThreeMFError("too many faces")
                parts.append((v @ matrix[:3, :3].T + matrix[:3, 3], f))
            for child_id, child_path, child_matrix in entry["components"]:
                target = child_path.lower().lstrip("/") if child_path else model_key
                walk(target, child_id, matrix @ child_matrix, depth + 1)

        main = models[main_name]
        for oid, matrix in main["build"]:
            walk(main_name, oid, matrix, 0)
        if not parts:                                         # no build list, or it led nowhere: every mesh object we can find
            for key, model in models.items():
                for oid, entry in model["objects"].items():
                    if entry["mesh"] is not None:
                        walk(key, oid, np.eye(4), 0)
    if not parts:
        raise ThreeMFError("no mesh in the file")
    vertices, faces, offset = [], [], 0
    for v, f in parts:
        vertices.append(v)
        faces.append(f + offset)
        offset += len(v)
    return trimesh.Trimesh(vertices=np.vstack(vertices), faces=np.vstack(faces), process=False)
