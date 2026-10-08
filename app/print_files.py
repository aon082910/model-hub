"""Sliced files kept with a model, and reading what the slicer wrote into them.

Nothing here runs a slicer. A file is G-code (or a sliced .3mf) that you made yourself; Model Hub stores it, reads the
estimated time, filament weight, layer height and filament type from the comments the slicer wrote, and can send it to
a printer. Files live in CONFIG_PATH/print_files and are not part of a backup (they can be large and can be made again).
"""
import re
import zipfile
from pathlib import Path
from typing import Optional

from lxml import etree

from app.config import CONFIG_PATH

PRINT_DIR = CONFIG_PATH / "print_files"
GCODE_KINDS = ("gcode", "gco", "g", "bgcode")
KINDS = GCODE_KINDS + ("3mf",)
MAX_BYTES = 1024 ** 3
HEAD_TAIL_BYTES = 96 * 1024
_DURATION = re.compile(r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?", re.I)


def stored_path(name: str) -> Path:
    return PRINT_DIR / name


def parse_duration(text: str) -> Optional[float]:
    """'1d 2h 3m 4s' (any part optional) -> minutes."""
    match = _DURATION.fullmatch((text or "").strip())
    if not match or not any(match.groups()):
        return None
    d, h, m, s = (float(g) if g else 0.0 for g in match.groups())
    return round(d * 1440 + h * 60 + m + s / 60, 1)


def _first(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text, re.I | re.M)
    return m.group(1).strip() if m else None


def parse_gcode_text(text: str) -> dict:
    """What PrusaSlicer, OrcaSlicer, Bambu Studio and Cura write in their G-code comments."""
    slicer = _first(r"^\s*;\s*generated (?:by|with) ([^\n]{2,60}?)(?:\s+on\s+\d|\s*$)", text)
    seconds = _first(r"^\s*;TIME:\s*(\d+)", text)                                  # Cura
    minutes = round(int(seconds) / 60, 1) if seconds else None
    if minutes is None:
        for pattern in (r"^\s*;\s*estimated printing time \(normal mode\)\s*=\s*([^\n]+)",   # Prusa / Orca
                        r"total estimated time:\s*([^\n;]+)"):                           # Bambu
            value = _first(pattern, text)
            minutes = parse_duration(value) if value else None
            if minutes is not None:
                break
    grams = _first(r"^\s*;\s*(?:total )?filament (?:used \[g\]|weight \[g\])\s*[=:]\s*([\d.]+)", text)
    filament = _first(r"^\s*;\s*filament_type\s*=\s*([^\n;]+)", text) or _first(r"^\s*;\s*filament_type\s*:\s*([^\n;]+)", text)
    layer = _first(r"^\s*;\s*layer_height\s*[=:]\s*([\d.]+)", text) or _first(r"^\s*;\s*Layer height:\s*([\d.]+)", text)
    return {"slicer": slicer[:80] if slicer else None, "est_minutes": minutes,
            "est_grams": round(float(grams), 2) if grams else None,
            "filament_type": filament[:40] if filament else None, "layer_height": layer}


def parse_3mf(path: Path) -> dict:
    """A sliced Bambu/Orca 3MF carries Metadata/slice_info.config; an ordinary 3MF carries nothing we read."""
    out = {"slicer": None, "est_minutes": None, "est_grams": None, "filament_type": None, "layer_height": None}
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("Metadata/slice_info.config")
            if info.file_size > 2 * 1024 * 1024:
                return out
            data = z.read(info)
    except (KeyError, zipfile.BadZipFile, OSError):
        return out
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))
    except etree.XMLSyntaxError:
        return out
    meta = {m.get("key"): m.get("value") for m in root.iter("metadata")}
    try:
        if meta.get("prediction"):
            out["est_minutes"] = round(float(meta["prediction"]) / 60, 1)
        if meta.get("weight"):
            out["est_grams"] = round(float(meta["weight"]), 2)
    except ValueError:
        pass
    types = [f.get("type") for f in root.iter("filament") if f.get("type")]
    out["filament_type"] = types[0][:40] if types else None
    if meta.get("client_type") or meta.get("client_version"):
        out["slicer"] = f"{(meta.get('client_type') or 'slicer')} {(meta.get('client_version') or '')}".strip()[:80]
    return out


def read_metadata(path: Path, kind: str) -> dict:
    """Best-effort: a file that cannot be read just has no metadata."""
    empty = {"slicer": None, "est_minutes": None, "est_grams": None, "filament_type": None, "layer_height": None}
    try:
        if kind == "3mf":
            return parse_3mf(path)
        if kind == "bgcode":
            return empty                                      # binary G-code: stored and sent, not read
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(HEAD_TAIL_BYTES)
            tail = b""
            if size > HEAD_TAIL_BYTES:
                f.seek(max(HEAD_TAIL_BYTES, size - HEAD_TAIL_BYTES))
                tail = f.read(HEAD_TAIL_BYTES)
        return parse_gcode_text((head + b"\n" + tail).decode("utf-8", errors="ignore"))
    except OSError:
        return empty
