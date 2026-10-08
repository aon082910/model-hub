"""Talking to your printers: see what they are doing and send them a sliced file.

Three kinds of printer are supported, all over your own network:
  * Klipper / Moonraker (Mainsail, Fluidd, ...): the Moonraker HTTP API.
  * OctoPrint: its REST API (needs its API key).
  * Bambu Lab in LAN mode (P1, X1, A1...): the printer's own MQTT broker, with its serial number and LAN access code. Status and the AMS
    are read; files are not sent (use Bambu Studio for that).
Printers print G-code, not models, so what is sent is either a G-code file you choose, or a model
sliced here with a headless slicer (SLICER_CLI_PATH) and the printer profile you exported
(SLICER_CONFIG_PATH). Without both, slicing is not offered: a default profile would not match your
machine, and sending the wrong G-code to a printer can damage it.

Starting a print is always something you ask for; nothing is ever started by itself. API keys are
never put in a response or an error message.
"""
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger("modelhub.printers")

KINDS = ("moonraker", "octoprint", "bambu")
BAMBU_PORT = 8883
BAMBU_TIMEOUT = 8.0
GCODE_EXTENSIONS = {".gcode", ".gco", ".g", ".bgcode"}
SLICEABLE_EXTENSIONS = {".stl", ".3mf", ".obj", ".step", ".stp"}
MAX_UPLOAD_BYTES = 1024 ** 3
TIMEOUT = httpx.Timeout(10.0, read=30.0)
UPLOAD_TIMEOUT = httpx.Timeout(10.0, read=600.0, write=600.0)
SLICE_TIMEOUT_SECONDS = 600


class PrinterError(Exception):
    """Something to tell the user about (never contains a key)."""


def slicer_ready() -> bool:
    cli, config = os.environ.get("SLICER_CLI_PATH"), os.environ.get("SLICER_CONFIG_PATH")
    return bool(cli and config and Path(cli).is_file() and Path(config).is_file())


def slicer_note() -> str:
    if slicer_ready():
        return f"Models are sliced with {Path(os.environ['SLICER_CLI_PATH']).name} and your printer profile before they are sent."
    return ("Slicing here is off. To slice models for a printer, give the container a headless slicer "
            "(SLICER_CLI_PATH) and the printer profile you exported from it (SLICER_CONFIG_PATH); "
            "otherwise send a G-code file you made yourself.")


def clean_url(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PrinterError("Give the printer's address, like http://192.168.1.60:7125")
    url = value.strip()
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PrinterError("The address must start with http:// or https://")
    if parsed.username or parsed.password:
        raise PrinterError("Do not put a user name or password in the address")
    if parsed.query or parsed.fragment:
        raise PrinterError("The address should just be the printer's own address")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9]{8,24}$")


def clean_host(value) -> str:
    """The address of a Bambu printer: a host name or IP address, nothing else (the port is fixed)."""
    if not isinstance(value, str) or not value.strip():
        raise PrinterError("Give the printer's address on your network, like 192.168.1.60")
    host = re.sub(r"^[a-z]+://", "", value.strip()).split("/")[0].split(":")[0]
    if re.search(r"\s", value.strip()) or ".." in value or not _HOST_RE.match(host):
        raise PrinterError("The address must be a host name or an IP address like 192.168.1.60")
    return host


def clean_serial(value) -> str:
    if not isinstance(value, str) or not _SERIAL_RE.match(value.strip()):
        raise PrinterError("Give the printer's serial number (on its label, or in Bambu Studio's device page)")
    return value.strip().upper()


def clean_snapshot_url(value) -> Optional[str]:
    """The address of the printer camera's still picture (http://host/webcam/?action=snapshot), or None for none."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise PrinterError("The camera address must be text")
    url = value.strip()
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PrinterError("The camera address must start with http:// or https://")
    if parsed.username or parsed.password:
        raise PrinterError("Do not put a user name or password in the camera address")
    if parsed.fragment or len(url) > 500:
        raise PrinterError("That camera address is not usable")
    return url


MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


def fetch_snapshot(url: str) -> bytes:
    """One still picture from the printer's camera. The printer's API key is never sent to the camera."""
    try:
        with _client() as client, client.stream("GET", url) as response:
            if 300 <= response.status_code < 400:
                raise PrinterError("The camera's address redirects somewhere else; use its real address")
            if response.status_code != 200:
                raise PrinterError(f"The camera answered with an error ({response.status_code})")
            if not response.headers.get("content-type", "").lower().startswith("image/"):
                raise PrinterError("The camera address did not give a picture")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_SNAPSHOT_BYTES:
                    raise PrinterError("The camera's picture is larger than 8 MB")
    except httpx.TimeoutException:
        raise PrinterError("The camera did not answer in time")
    except httpx.HTTPError as e:
        raise PrinterError(f"Could not reach the camera ({e.__class__.__name__})")
    if not data:
        raise PrinterError("The camera gave an empty picture")
    return bytes(data)


def list_timelapses(kind: str, url: str, api_key: Optional[str]) -> list:
    """The time-lapse videos a printer keeps: [{name, size, modified (epoch seconds or None), url}]. Raises PrinterError.
    Klipper needs the moonraker-timelapse plugin, OctoPrint its timelapse feature; a printer with neither has none."""
    if kind == "bambu":
        raise PrinterError("Bambu printers do not keep time-lapse videos where Model Hub can list them")
    base = urlparse(url)
    root = f"{base.scheme}://{base.netloc}"
    with _client() as client:
        if kind == "moonraker":
            response = _request(client, "GET", f"{url}/server/files/list", api_key, params={"root": "timelapse"})
            if response.status_code == 404:
                return []
            if response.status_code != 200:
                raise PrinterError(f"The printer answered with an error ({response.status_code})")
            found = [{"name": str(f.get("path")), "size": f.get("size"), "modified": f.get("modified"),
                      "url": f"{url}/server/files/timelapse/{quote(str(f.get('path')))}"}
                     for f in (_json(response).get("result") or []) if isinstance(f, dict) and str(f.get("path", "")).lower().endswith(VIDEO_EXTENSIONS)]
        else:
            response = _request(client, "GET", f"{url}/api/timelapse", api_key)
            if response.status_code != 200:
                raise PrinterError(f"The printer answered with an error ({response.status_code})")
            found = [{"name": str(f.get("name")), "size": f.get("bytes") or f.get("size"), "modified": None, "url": root + str(f.get("url"))}
                     for f in (_json(response).get("files") or []) if isinstance(f, dict) and str(f.get("url", "")).startswith("/")
                     and str(f.get("name", "")).lower().endswith(VIDEO_EXTENSIONS)]
    return found


VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".mov")


def _client(timeout=TIMEOUT) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": "ModelHub/printers"})


def _headers(api_key: Optional[str]) -> dict:
    return {"X-Api-Key": api_key} if api_key else {}


def _request(client: httpx.Client, method: str, url: str, api_key: Optional[str], **kwargs):
    try:
        response = client.request(method, url, headers=_headers(api_key), **kwargs)
    except httpx.TimeoutException:
        raise PrinterError("The printer did not answer in time")
    except httpx.HTTPError as e:
        raise PrinterError(f"Could not reach the printer ({e.__class__.__name__})")
    if response.status_code in (401, 403):
        raise PrinterError("The printer refused the API key")
    if 300 <= response.status_code < 400:
        raise PrinterError("The printer's address redirects somewhere else; use its real address")
    return response


def _json(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        raise PrinterError("The printer answered with something unexpected")
    return data if isinstance(data, dict) else {}


def _number(value) -> Optional[float]:
    return round(float(value), 1) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---------- status ----------

def status(kind: str, url: str, api_key: Optional[str], serial: Optional[str] = None) -> dict:
    """{online, state, progress (0-100), file, nozzle, bed, message}. Never raises for an unreachable printer.
    A Bambu printer's answer also has "ams": what it says is in each slot."""
    result = {"online": False, "state": "offline", "progress": None, "file": None, "nozzle": None, "bed": None, "message": None,
              "duration": None}
    try:
        if kind == "bambu":
            return _bambu_status(url, serial, api_key, result)
        with _client() as client:
            if kind == "moonraker":
                return _moonraker_status(client, url, api_key, result)
            return _octoprint_status(client, url, api_key, result)
    except PrinterError as e:
        result["message"] = str(e)
        return result


def _bambu_report(host: str, serial: str, code: str, timeout: float = BAMBU_TIMEOUT) -> dict:
    """Ask a Bambu printer for its state over its LAN MQTT broker and return the "print" part of what it reports.
    (This is the one function that talks to the printer; tests replace it.) The printer's certificate is its own, so it cannot be
    checked against a public authority; the access code is what proves who we are."""
    import json
    import ssl
    import threading
    import paho.mqtt.client as mqtt

    merged, problem, got = {}, [], threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"modelhub-{serial[-6:]}")
    client.username_pw_set("bblp", code)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            problem.append("The printer refused the access code" if "uthoriz" in str(reason_code) else f"The printer refused the connection ({reason_code})")
            got.set()
            return
        c.subscribe(f"device/{serial}/report")
        c.publish(f"device/{serial}/request", json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}))

    def on_message(c, userdata, message):
        try:
            data = json.loads(message.payload.decode("utf-8", "replace"))
        except ValueError:
            return
        part = data.get("print") if isinstance(data, dict) else None
        if isinstance(part, dict):
            merged.update(part)
            if "gcode_state" in merged:
                got.set()

    client.on_connect, client.on_message = on_connect, on_message
    try:
        client.connect(host, BAMBU_PORT, keepalive=10)
        client.loop_start()
        got.wait(timeout)
    except OSError as e:
        raise PrinterError(f"Could not reach the printer ({e.__class__.__name__})")
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
    if problem:
        raise PrinterError(problem[0])
    if "gcode_state" not in merged:
        raise PrinterError("The printer did not answer (is LAN mode on, and are the serial number and access code right?)")
    return merged


_BAMBU_STATES = {"IDLE": "standby", "RUNNING": "printing", "PREPARE": "printing", "SLICING": "printing", "PAUSE": "paused",
                 "FINISH": "complete", "FAILED": "error"}


def _bambu_ams(report: dict) -> list:
    """The AMS trays as [{slot (1-based, four to a unit), material, color (#rrggbb), remain (percent or None), name, empty}]."""
    out = []
    for unit in ((report.get("ams") or {}).get("ams") or []):
        try:
            base = int(unit.get("id") or 0) * 4
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray") or []:
            try:
                slot = base + int(tray.get("id") or 0) + 1
            except (TypeError, ValueError):
                continue
            colour = str(tray.get("tray_color") or "")[:6]
            remain = tray.get("remain")
            out.append({"slot": slot, "material": (str(tray.get("tray_type") or "").strip() or None),
                        "color": "#" + colour.lower() if re.fullmatch(r"[0-9A-Fa-f]{6}", colour) else None,
                        "remain": remain if isinstance(remain, int) and not isinstance(remain, bool) and 0 <= remain <= 100 else None,
                        "name": (str(tray.get("tray_sub_brands") or "").strip() or None), "empty": not tray.get("tray_type")})
    return sorted(out, key=lambda t: t["slot"])


def _bambu_status(host, serial, code, result):
    if not host or not serial or not code:
        raise PrinterError("This printer needs its address, serial number and LAN access code")
    report = _bambu_report(host, serial, code)
    state = _BAMBU_STATES.get(str(report.get("gcode_state") or "").upper(), str(report.get("gcode_state") or "unknown").lower())
    percent = report.get("mc_percent")
    result.update(online=True, state=state, progress=float(percent) if isinstance(percent, (int, float)) and not isinstance(percent, bool) else None,
                  file=(report.get("subtask_name") or report.get("gcode_file") or None),
                  nozzle=_number(report.get("nozzle_temper")), bed=_number(report.get("bed_temper")), ams=_bambu_ams(report))
    return result


def _moonraker_status(client, url, api_key, result):
    response = _request(client, "GET", f"{url}/printer/objects/query", api_key,
                        params={"print_stats": "", "display_status": "", "extruder": "", "heater_bed": ""})
    if response.status_code == 503:
        result.update(online=True, state="klipper not ready", message="Klipper is not ready (check the printer's firmware state)")
        return result
    if response.status_code != 200:
        raise PrinterError(f"The printer answered with an error ({response.status_code})")
    objects = ((_json(response).get("result") or {}).get("status")) or {}
    stats, display = objects.get("print_stats") or {}, objects.get("display_status") or {}
    progress = display.get("progress")
    result.update(
        online=True, state=str(stats.get("state") or "unknown"), file=stats.get("filename") or None,
        progress=round(progress * 100, 1) if isinstance(progress, (int, float)) else None,
        duration=_number(stats.get("print_duration")),
        nozzle=_number((objects.get("extruder") or {}).get("temperature")),
        bed=_number((objects.get("heater_bed") or {}).get("temperature")),
    )
    return result


def _octoprint_status(client, url, api_key, result):
    response = _request(client, "GET", f"{url}/api/printer", api_key)
    if response.status_code == 409:
        result.update(online=True, state="not connected", message="OctoPrint is running but not connected to the printer")
        return result
    if response.status_code != 200:
        raise PrinterError(f"The printer answered with an error ({response.status_code})")
    data = _json(response)
    temps = data.get("temperature") or {}
    flags = (data.get("state") or {}).get("flags") or {}
    state = "printing" if flags.get("printing") else "paused" if flags.get("paused") else "ready" if flags.get("ready") else \
        str((data.get("state") or {}).get("text") or "unknown").lower()
    result.update(online=True, state=state, nozzle=_number((temps.get("tool0") or {}).get("actual")),
                  bed=_number((temps.get("bed") or {}).get("actual")))
    job = _request(client, "GET", f"{url}/api/job", api_key)
    if job.status_code == 200:
        info = _json(job)
        completion = (info.get("progress") or {}).get("completion")
        result.update(file=((info.get("job") or {}).get("file") or {}).get("name") or None,
                      progress=round(completion, 1) if isinstance(completion, (int, float)) else None)
    return result


# ---------- sending a file ----------

def safe_gcode_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._") or "model"
    suffix = Path(name).suffix.lower()
    return f"{stem[:80]}{suffix if suffix in GCODE_EXTENSIONS else '.gcode'}"


def send_file(kind: str, url: str, api_key: Optional[str], path: Path, filename: str, start: bool) -> dict:
    """Upload a G-code file; start it only when start is true. Returns {"filename", "started"}."""
    if kind == "bambu":
        raise PrinterError("Files cannot be sent to a Bambu printer from here; send them from Bambu Studio")
    if path.suffix.lower() not in GCODE_EXTENSIONS and Path(filename).suffix.lower() not in GCODE_EXTENSIONS:
        raise PrinterError("That is not a G-code file (.gcode, .gco, .g or .bgcode)")
    size = path.stat().st_size
    if size == 0:
        raise PrinterError("That file is empty")
    if size > MAX_UPLOAD_BYTES:
        raise PrinterError("That file is larger than 1 GB")
    with _client(UPLOAD_TIMEOUT) as client, open(path, "rb") as handle:
        if kind == "moonraker":
            response = _request(client, "POST", f"{url}/server/files/upload", api_key,
                                data={"root": "gcodes", "print": "true" if start else "false"},
                                files={"file": (filename, handle, "application/octet-stream")})
            ok = response.status_code in (200, 201)
            started = bool(_json(response).get("print_started")) if ok else False
        else:
            response = _request(client, "POST", f"{url}/api/files/local", api_key,
                                data={"select": "true", "print": "true" if start else "false"},
                                files={"file": (filename, handle, "application/octet-stream")})
            ok = response.status_code in (200, 201)
            started = start and ok
    if response.status_code == 409:
        raise PrinterError("The printer cannot take that file right now (is it busy printing, or not connected?)")
    if response.status_code == 413:
        raise PrinterError("The printer refused the file as too large")
    if not ok:
        raise PrinterError(f"The printer did not accept the file ({response.status_code})")
    if start and not started:
        raise PrinterError("The file was uploaded but the printer did not start it")
    return {"filename": filename, "started": started}


# ---------- slicing ----------

def slice_model(model_path: Path, workdir: Path, infill: float = 0.15) -> Path:
    """Slice a model to G-code with the configured slicer and printer profile."""
    if not slicer_ready():
        raise PrinterError(slicer_note())
    if model_path.suffix.lower() not in SLICEABLE_EXTENSIONS:
        raise PrinterError(f"{model_path.suffix or 'That'} files cannot be sliced here")
    out = workdir / "sliced.gcode"
    command = [os.environ["SLICER_CLI_PATH"], "--export-gcode", "--load", os.environ["SLICER_CONFIG_PATH"],
               "--fill-density", f"{int(infill * 100)}%", "-o", str(out), str(model_path)]
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=SLICE_TIMEOUT_SECONDS, check=True, cwd=str(workdir))
    except subprocess.TimeoutExpired:
        raise PrinterError("Slicing took too long")
    except (subprocess.CalledProcessError, OSError):
        raise PrinterError("The slicer could not slice that model (check it and your printer profile)")
    if not out.is_file() or out.stat().st_size == 0:
        raise PrinterError("The slicer did not produce any G-code")
    return out


def temp_dir() -> tempfile.TemporaryDirectory:
    from app.config import CONFIG_PATH
    base = CONFIG_PATH / "printing"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base)
