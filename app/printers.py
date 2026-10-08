"""Talking to your printers: see what they are doing and send them a sliced file.

Two kinds of printer are supported, both over your own network:
  * Klipper / Moonraker (Mainsail, Fluidd, ...): the Moonraker HTTP API.
  * OctoPrint: its REST API (needs its API key).
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
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("modelhub.printers")

KINDS = ("moonraker", "octoprint")
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

def status(kind: str, url: str, api_key: Optional[str]) -> dict:
    """{online, state, progress (0-100), file, nozzle, bed, message}. Never raises for an unreachable printer."""
    result = {"online": False, "state": "offline", "progress": None, "file": None, "nozzle": None, "bed": None, "message": None,
              "duration": None}
    try:
        with _client() as client:
            if kind == "moonraker":
                return _moonraker_status(client, url, api_key, result)
            return _octoprint_status(client, url, api_key, result)
    except PrinterError as e:
        result["message"] = str(e)
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
