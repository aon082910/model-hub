"""Sliced files kept with a model, and reading what the slicer wrote into them.

Nothing here runs a slicer. A file is G-code (or a sliced .3mf) that you made yourself; Model Hub stores it, reads the
estimated time, filament weight, layer height and filament type from the comments the slicer wrote, and can send it to
a printer. Files live in CONFIG_PATH/print_files and are not part of a backup (they can be large and can be made again).
"""
import json
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


_COLOUR = re.compile(r"#?([0-9A-Fa-f]{6})")


def _colour(value) -> Optional[str]:
    match = _COLOUR.search(str(value or ""))
    return "#" + match.group(1).lower() if match else None


def _filaments_json(rows: list) -> Optional[str]:
    rows = [r for r in rows if r.get("grams") and r["grams"] > 0][:16]
    return json.dumps(rows) if len(rows) > 0 else None


def _filaments_from_gcode(text: str) -> Optional[str]:
    """One entry per filament the file uses: its type, colour and grams (PrusaSlicer and OrcaSlicer list them as a;b;c and a, b, c)."""
    used = _first(r"^\s*;\s*(?:total )?filament used \[g\]\s*[=:]\s*([^\n]+)", text)
    if not used:
        return None
    grams = []
    for part in re.split(r"[,;]", used):
        try:
            grams.append(float(part.strip()))
        except ValueError:
            pass
    types = [t.strip().strip('"') for t in (_first(r"^\s*;\s*filament_type\s*[=:]\s*([^\n]+)", text) or "").split(";")]
    colours = [_colour(c) for c in re.split(r"[;,]", _first(r"^\s*;\s*filament_colou?r\s*[=:]\s*([^\n]+)", text) or "")]
    return _filaments_json([{"index": i + 1, "type": (types[i] if i < len(types) and types[i] else None),
                             "color": colours[i] if i < len(colours) else None, "grams": round(g, 2)} for i, g in enumerate(grams)])


DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PA": 1.14, "NYLON": 1.14, "PC": 1.2, "PVA": 1.23, "HIPS": 1.04, "PP": 0.9}


def grams_from_length(mm: float, filament_type: Optional[str] = None, diameter: float = 1.75) -> Optional[float]:
    """Weight of a length of filament: its volume (a cylinder of the filament's diameter) times the material's density."""
    if not isinstance(mm, (int, float)) or mm <= 0 or not 0.5 <= diameter <= 4:
        return None
    key = (filament_type or "").upper().replace("-", "").replace(" ", "")
    density = next((d for name, d in DENSITY.items() if key.startswith(name)), 1.24)
    cm3 = 3.141592653589793 * (diameter / 2) ** 2 * mm / 1000
    return round(cm3 * density, 2)


def _length_mm(text: str) -> Optional[float]:
    """Millimetres of filament the slicer says the print uses (PrusaSlicer and Orca: [mm], one per extruder; Cura: metres)."""
    used = _first(r"^\s*;\s*(?:total )?filament used \[mm\]\s*[=:]\s*([^\n]+)", text)
    if used:
        values = []
        for part in re.split(r"[,;]", used):
            try:
                values.append(float(part.strip()))
            except ValueError:
                pass
        if values:
            return sum(values)
    metres = _first(r"^\s*;\s*Filament used:\s*([\d.]+)\s*m\b", text)
    return float(metres) * 1000 if metres else None


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
    if not grams:                                              # no weight written: work it out from the length of filament
        length = _length_mm(text)
        diameter = _first(r"^\s*;\s*filament_diameter\s*[=:]\s*([\d.]+)", text)
        worked = grams_from_length(length, filament, float(diameter) if diameter else 1.75) if length else None
        grams = str(worked) if worked else None
    return {"slicer": slicer[:80] if slicer else None, "est_minutes": minutes,
            "est_grams": round(float(grams), 2) if grams else None,
            "filament_type": filament[:40] if filament else None, "layer_height": layer,
            "filaments": _filaments_from_gcode(text)}


def parse_3mf(path: Path) -> dict:
    """A sliced Bambu/Orca 3MF carries Metadata/slice_info.config; an ordinary 3MF carries nothing we read."""
    out = {"slicer": None, "est_minutes": None, "est_grams": None, "filament_type": None, "layer_height": None, "filaments": None}
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
    found = []
    for f in root.iter("filament"):
        try:
            grams = float(f.get("used_g") or 0)
            index = int(f.get("id") or len(found) + 1)
        except ValueError:
            continue
        found.append({"index": index, "type": (f.get("type") or "")[:40] or None, "color": _colour(f.get("color")), "grams": round(grams, 2)})
    out["filaments"] = _filaments_json(sorted(found, key=lambda r: r["index"]))
    types = [f.get("type") for f in root.iter("filament") if f.get("type")]
    out["filament_type"] = types[0][:40] if types else None
    if meta.get("client_type") or meta.get("client_version"):
        out["slicer"] = f"{(meta.get('client_type') or 'slicer')} {(meta.get('client_version') or '')}".strip()[:80]
    return out


def read_metadata(path: Path, kind: str) -> dict:
    """Best-effort: a file that cannot be read just has no metadata."""
    empty = {"slicer": None, "est_minutes": None, "est_grams": None, "filament_type": None, "layer_height": None, "filaments": None}
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
