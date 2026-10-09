"""What Model Hub can do with each kind of file, said plainly.

Meshes it opens (preview picture, 3D view, checks); STEP it shows but cannot check; anything else you may keep with a model as a stored file (a CAD project, a slicer project,
a Blender scene): it is kept and downloadable, never opened."""
import re

OPENED = {
    "stl": {"label": "STL", "preview": True, "viewer": True, "analyse": True},
    "obj": {"label": "Wavefront OBJ", "preview": True, "viewer": True, "analyse": True},
    "3mf": {"label": "3MF", "preview": True, "viewer": True, "analyse": True, "note": "slicer projects with several parts are read too; a file that cannot be read still gets indexed"},
    "fbx": {"label": "FBX", "preview": True, "viewer": True, "analyse": True},
    "step": {"label": "STEP", "preview": True, "viewer": True, "analyse": False, "note": "shown by your browser; no mesh checks or repair"},
    "stp": {"label": "STEP", "preview": True, "viewer": True, "analyse": False, "note": "shown by your browser; no mesh checks or repair"},
}
STORED = {
    "f3d": "Fusion 360", "f3z": "Fusion 360", "blend": "Blender", "scad": "OpenSCAD", "skp": "SketchUp", "sldprt": "SolidWorks part", "sldasm": "SolidWorks assembly",
    "ipt": "Autodesk Inventor part", "iam": "Autodesk Inventor assembly", "dwg": "AutoCAD drawing", "dxf": "DXF drawing", "fcstd": "FreeCAD", "lys": "Lychee Slicer",
    "ctb": "Chitubox", "cbddlp": "Chitubox", "photon": "Anycubic Photon", "dlp": "DLP slicer", "amf": "AMF", "x3d": "X3D", "ma": "Maya", "mb": "Maya", "max": "3ds Max",
    "c4d": "Cinema 4D", "mix": "Meshmixer", "hueforge": "HueForge", "gcode": "G-code", "bgcode": "Binary G-code", "ini": "Slicer settings", "json": "Settings (JSON)",
    "pdf": "Document", "txt": "Text", "md": "Notes", "png": "Picture", "jpg": "Picture", "jpeg": "Picture", "zip": "Archive", "svg": "Vector drawing",
}
DENIED = {"exe", "dll", "bat", "cmd", "com", "msi", "sh", "ps1", "vbs", "js", "jar", "html", "htm", "php", "py", "scr", "lnk", "apk", "app", "dmg"}


def extension_of(filename: str) -> str:
    return (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()


def describe_attachment(ext: str) -> dict:
    return {"label": STORED.get(ext) or (ext.upper() + " file" if ext else "File"), "capability": "stored only: kept with the model and downloadable, never opened"}


def table() -> dict:
    return {"opened": [{"extension": k, **v} for k, v in OPENED.items() if k != "stp"], "stored": [{"extension": k, "label": v} for k, v in sorted(STORED.items())],
            "refused": sorted(DENIED)}


def safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).strip(". ") or "file"
    return stem[:150]
