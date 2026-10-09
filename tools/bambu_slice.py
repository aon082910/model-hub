"""Slice a model to a Bambu `.gcode.3mf` with the Bambu Studio installed on this computer, headless, ready for Model-Hub to send.

Why this exists: Bambu Studio's command line does not resolve a profile's `"inherits"` chain when it is given the profile by path.
It quietly falls back to defaults, which for a P1S means a 200 x 200 x 100 bed, filament type PLA with density 0, and 2 walls.
This resolves each profile (machine, process, filament) into one standalone file first, applies your overrides, runs
`bambu-studio --slice`, and fills in the printer model id Studio's CLI leaves empty.

It runs on the computer that has Bambu Studio (Windows by default). Model-Hub in Docker on Unraid cannot run it: slice here,
upload the result to the model in Model-Hub as a kept sliced file, then send it with POST /api/printers/{id}/send-3mf.

    python tools/bambu_slice.py part.stl --out sliced --filament "Bambu PETG Basic @BBL X1C" --set wall_loops=7
Environment: BAMBU_STUDIO_EXE, BAMBU_PROFILES (default to the standard Windows install paths).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

DEFAULT_EXE = r"C:\Program Files\Bambu Studio\bambu-studio.exe"
DEFAULT_PROFILES = r"C:\Program Files\Bambu Studio\resources\profiles\BBL"
KINDS = ("machine", "process", "filament")
BED_TYPES = ("Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate")
# Studio's own `model_id` for each machine (from its machine profile); the CLI leaves printer_model_id empty in slice_info.config.
MODEL_IDS = {"Bambu Lab P1S": "C12", "Bambu Lab P1P": "C11"}
SLICEABLE = (".stl", ".3mf", ".step", ".stp", ".obj")


class SliceError(Exception):
    """Something to tell the person running this, in plain words."""


# ---------- profiles ----------

def profiles_dir() -> Path:
    return Path(os.environ.get("BAMBU_PROFILES", DEFAULT_PROFILES))


def build_index(root: Path) -> dict:
    """(kind, profile name) -> file, for every profile under root/machine, root/process and root/filament."""
    index = {}
    for kind in KINDS:
        for f in (root / kind).glob("*.json"):
            try:
                name = json.loads(f.read_text(encoding="utf-8")).get("name")
            except (OSError, ValueError):
                continue
            if name:
                index[(kind, name)] = f
    return index


def resolve(index: dict, kind: str, name: str, _seen: tuple = ()) -> dict:
    """The profile with its whole `inherits` chain folded in: parent first, child on top."""
    if (kind, name) in _seen:
        raise SliceError(f"The {kind} profile {name!r} inherits from itself")
    path = index.get((kind, name))
    if path is None:
        raise SliceError(f"There is no {kind} profile named {name!r} in Bambu Studio's profiles")
    data = json.loads(path.read_text(encoding="utf-8"))
    parent = data.get("inherits")
    base = resolve(index, kind, parent, _seen + ((kind, name),)) if parent else {}
    merged = {**base, **{k: v for k, v in data.items() if k != "inherits"}}
    merged["from"] = "system"
    merged["name"] = name
    return merged


def flatten(root: Path, out: Path, machine: str, process: str, filament: str, overrides: dict = None) -> dict:
    """Write standalone machine/process/filament JSON files into out; overrides (key -> value, written as text) go on the process."""
    out.mkdir(parents=True, exist_ok=True)
    index = build_index(root)
    if not index:
        raise SliceError(f"No Bambu Studio profiles found under {root} (set BAMBU_PROFILES)")
    paths = {}
    for kind, name in zip(KINDS, (machine, process, filament)):
        data = resolve(index, kind, name)
        if kind == "process" and overrides:
            data.update({k: str(v) for k, v in overrides.items()})
        paths[kind] = out / f"{kind}_flat.json"
        paths[kind].write_text(json.dumps(data, indent=2), encoding="utf-8")
    return paths


# ---------- the sliced file ----------

def patch_printer_model_id(path: Path, model_id: str) -> bool:
    """Put the printer's model id into slice_info.config when the CLI left it empty. Returns True if the file was changed.
    Only that one member changes; the G-code and its checksum are untouched."""
    pattern = re.compile(r'(key="printer_model_id"\s+value=")(")')
    with zipfile.ZipFile(path) as src:
        try:
            info = src.read("Metadata/slice_info.config").decode("utf-8")
        except KeyError:
            return False
        if not pattern.search(info):
            return False
        patched = pattern.sub(lambda m: m.group(1) + model_id + m.group(2), info, count=1).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(suffix=".3mf", dir=str(path.parent))
        os.close(fd)
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                dst.writestr(item, patched if item.filename == "Metadata/slice_info.config" else src.read(item.filename))
    os.replace(tmp_name, path)
    return True


def summarize(path: Path) -> dict:
    """Print time, filament and bed as recorded in the sliced file."""
    with zipfile.ZipFile(path) as z:
        info = z.read("Metadata/slice_info.config").decode("utf-8", "replace")
        settings = json.loads(z.read("Metadata/project_settings.config"))
    seconds = re.search(r'key="prediction"\s+value="(\d+)"', info)
    fil = re.search(r'<filament [^>]*type="([^"]*)"[^>]*used_g="([\d.]+)"', info)
    return {"file": str(path), "minutes": round(int(seconds.group(1)) / 60, 1) if seconds else None,
            "filament_type": fil.group(1) if fil else None, "grams": float(fil.group(2)) if fil else None,
            "bed": settings.get("printable_area"), "bed_height": settings.get("printable_height"),
            "bed_type": settings.get("curr_bed_type"), "walls": settings.get("wall_loops"),
            "printer": settings.get("printer_settings_id"), "process": settings.get("print_settings_id")}


# ---------- slicing ----------

def find_studio() -> Path:
    exe = Path(os.environ.get("BAMBU_STUDIO_EXE", DEFAULT_EXE))
    if not exe.is_file():
        raise SliceError(f"Bambu Studio was not found at {exe} (set BAMBU_STUDIO_EXE)")
    return exe


def slice_model(model: Path, out_dir: Path, machine: str = "Bambu Lab P1S 0.4 nozzle", process: str = "0.20mm Standard @BBL X1C",
                filament: str = "Bambu PETG Basic @BBL X1C", overrides: dict = None, bed_type: str = "Textured PEI Plate",
                orient: bool = False, arrange: bool = True, timeout: int = 600, exe: Path = None) -> Path:
    """Slice one model into out_dir/<name>.gcode.3mf and return that path. Raises SliceError with the reason otherwise."""
    model = Path(model).resolve()                 # Studio is started from a temp folder, so relative paths would not find the file
    if not model.is_file():
        raise SliceError(f"{model} does not exist")
    if model.suffix.lower() not in SLICEABLE:
        raise SliceError(f"{model.suffix or 'That'} files cannot be sliced (use {', '.join(SLICEABLE)})")
    if bed_type not in BED_TYPES:
        raise SliceError(f"The bed type must be one of: {', '.join(BED_TYPES)}")
    exe = Path(exe).resolve() if exe else find_studio().resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    result_name = f"{model.stem}.gcode.3mf"
    target = out_dir / result_name
    with tempfile.TemporaryDirectory(prefix="bambu-slice-") as tmp:
        work = Path(tmp)
        studio_out = work / "studio_out"           # Studio also writes a raw plate_1.gcode and result.json; keep those here
        flat = flatten(profiles_dir(), work, machine, process, filament, overrides)
        command = [str(exe), "--debug", "2", "--arrange", "1" if arrange else "0", "--orient", "1" if orient else "0",
                   "--curr-bed-type", bed_type,
                   "--load-settings", f"{flat['machine']};{flat['process']}", "--load-filaments", str(flat["filament"]),
                   "--slice", "0", "--outputdir", str(studio_out), "--export-3mf", result_name, str(model)]
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise SliceError(f"Bambu Studio took longer than {timeout} s")
        except OSError as e:
            raise SliceError(f"Could not start Bambu Studio ({e.__class__.__name__})")
        reason = ""
        result = studio_out / "result.json"
        if result.is_file():
            try:
                reason = str(json.loads(result.read_text(encoding="utf-8")).get("error_string") or "")
            except ValueError:
                pass
        made = studio_out / result_name
        if run.returncode != 0 or not made.is_file() or made.stat().st_size == 0:
            raise SliceError(f"Bambu Studio could not slice that model ({reason or 'exit code ' + str(run.returncode)}). "
                             "Check the model is one solid mesh and fits the bed")
        shutil.move(str(made), str(target))
    model_id = MODEL_IDS.get(re.sub(r"\s+\d+(\.\d+)? nozzle$", "", machine))
    if model_id:
        patch_printer_model_id(target, model_id)
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Slice a model to a Bambu .gcode.3mf with the local Bambu Studio")
    ap.add_argument("model", type=Path)
    ap.add_argument("--out", type=Path, default=Path("sliced"))
    ap.add_argument("--machine", default="Bambu Lab P1S 0.4 nozzle")
    ap.add_argument("--process", default="0.20mm Standard @BBL X1C")
    ap.add_argument("--filament", default="Bambu PETG Basic @BBL X1C")
    ap.add_argument("--bed", default="Textured PEI Plate", choices=BED_TYPES)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a process setting, e.g. wall_loops=7")
    ap.add_argument("--orient", action="store_true", help="let Studio choose the orientation (off: keep the model as it is)")
    ap.add_argument("--no-arrange", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args(argv)
    try:
        overrides = dict(item.split("=", 1) for item in args.set)
        path = slice_model(args.model, args.out, args.machine, args.process, args.filament, overrides, args.bed,
                           args.orient, not args.no_arrange, args.timeout)
        print(json.dumps(summarize(path), indent=2))
        return 0
    except ValueError:
        print("--set needs KEY=VALUE", file=sys.stderr)
        return 2
    except SliceError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
