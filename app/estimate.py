"""Print time / filament weight estimation.

Two modes:
- Heuristic (always available): a volumetric approximation based on the mesh's
  volume, a chosen infill fraction, and a rough shell-thickness model. This is
  NOT a real slice -- no supports, no travel optimization, no per-layer
  geometry -- just a physically-reasoned estimate, clearly labeled as such.
- Exact (opt-in): if SLICER_CLI_PATH points at a headless slicer binary
  (e.g. a PrusaSlicer/OrcaSlicer AppImage extracted with --appimage-extract,
  bind-mounted into the container), the model is actually sliced and the
  real numbers are parsed out of the generated G-code header.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

MATERIAL_DENSITY_G_CM3 = {
    "PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "TPU": 1.21, "Nylon": 1.14,
}

SLICER_CLI_PATH = os.environ.get("SLICER_CLI_PATH")

WALL_LOOPS = 3
NOZZLE_DIAMETER_MM = 0.4
LAYER_HEIGHT_MM = 0.2
PRINT_SPEED_MM_S = 60
TRAVEL_OVERHEAD_FACTOR = 1.35  # fudge for travel moves, retraction, acceleration


def minutes_for_grams(grams: float, material: str = "PLA") -> float:
    """Rough print time for a given amount of filament, on the same assumptions as the volumetric
    estimate (so a project's electricity cost can be worked out from the grams it uses)."""
    density = MATERIAL_DENSITY_G_CM3.get(material, MATERIAL_DENSITY_G_CM3["PLA"])
    volume_mm3 = (max(0.0, grams) / density) * 1000
    flow_rate_mm3_s = LAYER_HEIGHT_MM * NOZZLE_DIAMETER_MM * PRINT_SPEED_MM_S
    return (volume_mm3 / flow_rate_mm3_s) * TRAVEL_OVERHEAD_FACTOR / 60


def _heuristic_estimate(volume_mm3: float, surface_area_mm2: float, material: str, infill: float) -> dict:
    density = MATERIAL_DENSITY_G_CM3.get(material, MATERIAL_DENSITY_G_CM3["PLA"])

    wall_thickness_mm = WALL_LOOPS * NOZZLE_DIAMETER_MM
    shell_volume_mm3 = min(volume_mm3, surface_area_mm2 * wall_thickness_mm)
    interior_volume_mm3 = max(volume_mm3 - shell_volume_mm3, 0)
    material_volume_mm3 = shell_volume_mm3 + interior_volume_mm3 * max(0, min(infill, 1))

    mass_g = (material_volume_mm3 / 1000) * density  # mm3 -> cm3 -> g

    flow_rate_mm3_s = LAYER_HEIGHT_MM * NOZZLE_DIAMETER_MM * PRINT_SPEED_MM_S
    seconds = (material_volume_mm3 / flow_rate_mm3_s) * TRAVEL_OVERHEAD_FACTOR

    return {
        "source": "heuristic",
        "material": material,
        "infill": infill,
        "estimated_grams": round(mass_g, 1),
        "estimated_minutes": round(seconds / 60, 1),
        "note": "Volumetric approximation, not a real slice. Accuracy is rough "
                "(typically within ~30-50% for simple shapes) -- set SLICER_CLI_PATH "
                "for exact numbers from a real slicer.",
    }


_TIME_RE = re.compile(r"estimated printing time.*?=\s*(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", re.I)
_FILAMENT_G_RE = re.compile(r"filament used \[g\]\s*=\s*([\d.]+)", re.I)


def _slicer_estimate(file_path: Path, material: str, infill: float) -> Optional[dict]:
    if not SLICER_CLI_PATH or not Path(SLICER_CLI_PATH).exists():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out_gcode = Path(tmp) / "out.gcode"
        try:
            subprocess.run(
                [
                    SLICER_CLI_PATH, "--export-gcode",
                    "--fill-density", str(int(infill * 100)),
                    "-o", str(out_gcode),
                    str(file_path),
                ],
                capture_output=True, text=True, timeout=180, check=True,
            )
        except Exception:
            return None
        if not out_gcode.exists():
            return None

        header = out_gcode.read_text(errors="ignore")[:20000]
        grams = None
        minutes = None
        m = _FILAMENT_G_RE.search(header)
        if m:
            grams = float(m.group(1))
        m = _TIME_RE.search(header)
        if m:
            h, mi, s = (int(g) if g else 0 for g in m.groups())
            minutes = h * 60 + mi + s / 60

        if grams is None and minutes is None:
            return None
        return {
            "source": "slicer",
            "material": material,
            "infill": infill,
            "estimated_grams": grams,
            "estimated_minutes": round(minutes, 1) if minutes else None,
            "note": f"Exact slice via {Path(SLICER_CLI_PATH).name}.",
        }


def estimate_print(model, file_path: Path, material: str = "PLA", infill: float = 0.15) -> dict:
    exact = _slicer_estimate(file_path, material, infill)
    if exact:
        return exact

    if not model.volume_mm3:
        return {
            "source": "unavailable",
            "material": material,
            "infill": infill,
            "estimated_grams": None,
            "estimated_minutes": None,
            "note": "No volume data for this file (mesh failed to parse, or it's a "
                    "non-mesh format like STEP). Rescan or check the file.",
        }

    # crude surface-area proxy from the bounding box when we don't have a mesh handle here
    surface_area_mm2 = 2 * (
        (model.bbox_x or 0) * (model.bbox_y or 0)
        + (model.bbox_y or 0) * (model.bbox_z or 0)
        + (model.bbox_x or 0) * (model.bbox_z or 0)
    )
    return _heuristic_estimate(model.volume_mm3, surface_area_mm2, material, infill)
