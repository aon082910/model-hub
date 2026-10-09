"""tools/bambu_slice.py: flattening Bambu Studio profiles, running its command line, patching the result.
Bambu Studio itself is never run here: a fake executable (a small Python script) stands in for it."""
import importlib.util
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("bambu_slice", Path(__file__).resolve().parent.parent / "tools" / "bambu_slice.py")
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


def write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def profiles(tmp_path):
    root = tmp_path / "BBL"
    write(root / "machine" / "common.json", {"name": "common", "printable_area": ["0x0", "200x0", "200x200", "0x200"], "printable_height": "100", "gcode_flavor": "marlin"})
    write(root / "machine" / "P1S 0.4.json", {"name": "P1S 0.4", "inherits": "common", "printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": "250"})
    write(root / "process" / "base.json", {"name": "base", "wall_loops": "2", "layer_height": "0.2"})
    write(root / "process" / "0.20 Std.json", {"name": "0.20 Std", "inherits": "base", "layer_height": "0.2"})
    write(root / "filament" / "pet base.json", {"name": "pet base", "filament_type": ["PETG"], "filament_density": ["1.25"]})
    write(root / "filament" / "PETG Basic.json", {"name": "PETG Basic", "inherits": "pet base", "nozzle_temperature": ["255"]})
    return root


def test_a_profile_is_folded_into_one_file_with_the_child_on_top(profiles, tmp_path):
    paths = bs.flatten(profiles, tmp_path / "flat", "P1S 0.4", "0.20 Std", "PETG Basic", {"wall_loops": 7})
    machine = json.loads(paths["machine"].read_text())
    assert machine["printable_area"][1] == "256x0" and machine["printable_height"] == "250"      # the child's, not the parent's
    assert machine["gcode_flavor"] == "marlin" and "inherits" not in machine and machine["from"] == "system"
    process = json.loads(paths["process"].read_text())
    assert process["wall_loops"] == "7" and process["layer_height"] == "0.2"                       # override as text
    filament = json.loads(paths["filament"].read_text())
    assert filament["filament_type"] == ["PETG"] and filament["filament_density"] == ["1.25"] and filament["nozzle_temperature"] == ["255"]
    assert filament["name"] == "PETG Basic"


def test_overrides_touch_only_the_process_file(profiles, tmp_path):
    paths = bs.flatten(profiles, tmp_path / "flat", "P1S 0.4", "0.20 Std", "PETG Basic", {"wall_loops": 7})
    assert "wall_loops" not in json.loads(paths["machine"].read_text())
    assert "wall_loops" not in json.loads(paths["filament"].read_text())


def test_missing_loops_and_empty_profile_folders_are_explained(profiles, tmp_path):
    with pytest.raises(bs.SliceError, match="no machine profile named|There is no machine profile"):
        bs.flatten(profiles, tmp_path / "f", "Nope", "0.20 Std", "PETG Basic")
    write(profiles / "process" / "a.json", {"name": "a", "inherits": "b"})
    write(profiles / "process" / "b.json", {"name": "b", "inherits": "a"})
    with pytest.raises(bs.SliceError, match="inherits from itself"):
        bs.flatten(profiles, tmp_path / "f", "P1S 0.4", "a", "PETG Basic")
    with pytest.raises(bs.SliceError, match="No Bambu Studio profiles"):
        bs.flatten(tmp_path / "empty", tmp_path / "f", "x", "y", "z")


def sliced_zip(path: Path, model_id_value="", gcode=b"G28\n"):
    info = ('<config><plate><metadata key="index" value="1"/><metadata key="printer_model_id" value="%s"/>'
            '<metadata key="prediction" value="7688"/>'
            '<filament id="1" type="PETG" color="#00AE42" used_m="23.9" used_g="29.5"/></plate></config>' % model_id_value)
    settings = {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": "250", "curr_bed_type": "Textured PEI Plate",
                "wall_loops": "7", "printer_settings_id": "Bambu Lab P1S 0.4 nozzle", "print_settings_id": "0.20mm Standard @BBL X1C"}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("Metadata/slice_info.config", info)
        z.writestr("Metadata/project_settings.config", json.dumps(settings))
        z.writestr("Metadata/plate_1.gcode", gcode)
        z.writestr("Metadata/plate_1.gcode.md5", "abc")


def test_an_empty_printer_model_id_is_filled_in_and_nothing_else_changes(tmp_path):
    f = tmp_path / "a.gcode.3mf"
    sliced_zip(f, "")
    before = {n: zipfile.ZipFile(f).read(n) for n in zipfile.ZipFile(f).namelist()}
    assert bs.patch_printer_model_id(f, "C12") is True
    with zipfile.ZipFile(f) as z:
        assert z.testzip() is None
        after = {n: z.read(n) for n in z.namelist()}
    assert b'key="printer_model_id" value="C12"' in after["Metadata/slice_info.config"]
    assert set(before) == set(after)
    for name in before:
        if name != "Metadata/slice_info.config":
            assert before[name] == after[name], name           # G-code and checksum untouched
    assert bs.patch_printer_model_id(f, "C12") is False        # second time: nothing to do


def test_a_model_id_that_is_already_there_is_left_alone(tmp_path):
    f = tmp_path / "a.gcode.3mf"
    sliced_zip(f, "BL-P001")
    assert bs.patch_printer_model_id(f, "C12") is False
    assert b"BL-P001" in zipfile.ZipFile(f).read("Metadata/slice_info.config")


def test_the_summary_reads_time_filament_and_bed(tmp_path):
    f = tmp_path / "a.gcode.3mf"
    sliced_zip(f, "C12")
    s = bs.summarize(f)
    assert s["minutes"] == 128.1 and s["grams"] == 29.5 and s["filament_type"] == "PETG"
    assert s["bed"][2] == "256x256" and s["bed_type"] == "Textured PEI Plate" and s["walls"] == "7"


@pytest.fixture()
def fake_studio(tmp_path, monkeypatch, profiles):
    """A stand-in bambu-studio: records its arguments and writes a sliced 3MF plus result.json where it was told to."""
    log = tmp_path / "args.json"
    script = tmp_path / "fake_studio.py"
    script.write_text(f'''
import json, sys, zipfile
a = sys.argv[1:]
json.dump(a, open({str(log)!r}, "w"))
out = a[a.index("--outputdir") + 1]
name = a[a.index("--export-3mf") + 1]
import os
os.makedirs(out, exist_ok=True)
if not os.path.isfile(a[-1]):               # like the real thing: a model path that does not resolve from where it runs
    json.dump({{"error_string": "Failed to load the model", "return_code": -3}}, open(os.path.join(out, "result.json"), "w"))
    sys.exit(253)
if os.environ.get("FAKE_FAIL"):
    json.dump({{"error_string": "Nothing to slice", "return_code": 3}}, open(os.path.join(out, "result.json"), "w"))
    sys.exit(3)
info = '<config><plate><metadata key="printer_model_id" value=""/><metadata key="prediction" value="600"/><filament id="1" type="PETG" used_g="1.5"/></plate></config>'
with zipfile.ZipFile(os.path.join(out, name), "w") as z:
    z.writestr("Metadata/slice_info.config", info)
    z.writestr("Metadata/project_settings.config", json.dumps({{"printable_area": ["0x0","256x0","256x256","0x256"]}}))
    z.writestr("Metadata/plate_1.gcode", "G28")
json.dump({{"error_string": "Success.", "return_code": 0}}, open(os.path.join(out, "result.json"), "w"))
''', encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "fake_studio.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "fake_studio.sh"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BAMBU_PROFILES", str(profiles))
    return exe, log


def stl(tmp_path) -> Path:
    f = tmp_path / "part.stl"
    f.write_bytes(b"solid x\nendsolid x\n")
    return f


def test_the_command_has_the_flattened_profiles_the_bed_type_and_keeps_the_orientation(fake_studio, tmp_path):
    exe, log = fake_studio
    out = bs.slice_model(stl(tmp_path), tmp_path / "out", machine="P1S 0.4", process="0.20 Std", filament="PETG Basic",
                         overrides={"wall_loops": 7}, exe=exe)
    args = json.loads(log.read_text())
    assert out == tmp_path / "out" / "part.gcode.3mf" and out.is_file()
    assert [p.name for p in out.parent.iterdir()] == ["part.gcode.3mf"]       # Studio's raw G-code and result.json stay in the temp folder
    assert args[args.index("--orient") + 1] == "0" and args[args.index("--arrange") + 1] == "1"
    assert args[args.index("--curr-bed-type") + 1] == "Textured PEI Plate"
    assert args[args.index("--slice") + 1] == "0" and args[-1].endswith("part.stl")
    settings = args[args.index("--load-settings") + 1].split(";")
    assert [Path(p).name for p in settings] == ["machine_flat.json", "process_flat.json"]
    assert Path(args[args.index("--load-filaments") + 1]).name == "filament_flat.json"


def test_relative_paths_work_because_studio_is_started_from_a_temp_folder(fake_studio, tmp_path, monkeypatch):
    exe, _ = fake_studio
    stl(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = bs.slice_model(Path("part.stl"), Path("sliced"), machine="P1S 0.4", process="0.20 Std", filament="PETG Basic", exe=exe)
    assert out.is_file() and out.parent == (tmp_path / "sliced").resolve()


def test_a_slice_for_a_known_printer_gets_its_model_id(fake_studio, tmp_path, monkeypatch):
    exe, _ = fake_studio
    profiles_root = Path(os.environ["BAMBU_PROFILES"])
    write(profiles_root / "machine" / "Bambu Lab P1S 0.4 nozzle.json", {"name": "Bambu Lab P1S 0.4 nozzle", "printable_area": ["0x0"]})
    out = bs.slice_model(stl(tmp_path), tmp_path / "out", machine="Bambu Lab P1S 0.4 nozzle", process="0.20 Std", filament="PETG Basic", exe=exe)
    assert b'key="printer_model_id" value="C12"' in zipfile.ZipFile(out).read("Metadata/slice_info.config")


def test_a_failed_slice_says_why(fake_studio, tmp_path, monkeypatch):
    exe, _ = fake_studio
    monkeypatch.setenv("FAKE_FAIL", "1")
    with pytest.raises(bs.SliceError, match="Nothing to slice"):
        bs.slice_model(stl(tmp_path), tmp_path / "out", machine="P1S 0.4", process="0.20 Std", filament="PETG Basic", exe=exe)


def test_bad_inputs_are_refused_before_studio_is_started(fake_studio, tmp_path, monkeypatch):
    exe, log = fake_studio
    with pytest.raises(bs.SliceError, match="does not exist"):
        bs.slice_model(tmp_path / "missing.stl", tmp_path / "o", exe=exe)
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    with pytest.raises(bs.SliceError, match="cannot be sliced"):
        bs.slice_model(txt, tmp_path / "o", exe=exe)
    with pytest.raises(bs.SliceError, match="bed type"):
        bs.slice_model(stl(tmp_path), tmp_path / "o", bed_type="Glass", exe=exe)
    monkeypatch.setenv("BAMBU_STUDIO_EXE", str(tmp_path / "nowhere.exe"))
    with pytest.raises(bs.SliceError, match="not found"):
        bs.slice_model(stl(tmp_path), tmp_path / "o")
    assert not log.exists()


def test_the_command_line_tool_prints_a_summary_or_an_error(fake_studio, tmp_path, capsys, monkeypatch):
    exe, _ = fake_studio
    monkeypatch.setenv("BAMBU_STUDIO_EXE", str(exe))
    assert bs.main([str(stl(tmp_path)), "--out", str(tmp_path / "o"), "--machine", "P1S 0.4", "--process", "0.20 Std",
                    "--filament", "PETG Basic", "--set", "wall_loops=7"]) == 0
    assert json.loads(capsys.readouterr().out)["minutes"] == 10.0
    assert bs.main([str(tmp_path / "missing.stl")]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert bs.main([str(stl(tmp_path)), "--set", "oops"]) == 2
