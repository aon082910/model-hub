"""Send a sliced Bambu job (.gcode.3mf) to a Bambu printer in LAN mode, and start it only when asked.

How a Bambu printer takes a job (read from the community `bambulabs-api` 2.6.6 source; Bambu publishes no spec for it):
  1. The file is uploaded over implicit FTPS (port 990, user "bblp", the LAN access code as the password) to the
     root of the printer's microSD card. The data connection must reuse the control connection's TLS session.
  2. A `project_file` message on `device/<serial>/request` (MQTT, port 8883) tells it which plate of that file to print.
Since firmware 01.08.02.00 a P1S ignores those commands unless LAN Only + Developer Mode are on, so a start that the printer
does not act on is reported as such: the file is checked to have started by reading the printer's state afterwards.

Not yet tried on a real printer (the tests use fakes and one in-process FTPS server). Nothing here starts by itself:
`start` must be true, and a printer that is already busy is refused.
"""
import ftplib
import json
import re
import socket
import ssl
import time
import zipfile
from pathlib import Path
from typing import Optional

from app import printers
from app.printers import PrinterError

FTP_PORT = 990
MQTT_PORT = 8883
FTP_TIMEOUT = 30.0
MQTT_TIMEOUT = 8.0
START_WAIT_SECONDS = 25.0          # how long to wait for the printer to report that it has begun
START_POLL_SECONDS = 2.0
BUSY_STATES = ("printing", "paused")            # what printers._BAMBU_STATES maps RUNNING / PREPARE / SLICING / PAUSE to
BED_TYPES = ("auto", "cool_plate", "eng_plate", "hot_plate", "textured_plate")   # community-documented values
PLATE_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")
JOB_SUFFIX = ".gcode.3mf"


# ---------- reading the file ----------

def plate_numbers(path: Path) -> list:
    """The plates that have G-code inside a sliced 3MF; empty for an ordinary (unsliced) 3MF or anything else."""
    try:
        with zipfile.ZipFile(path) as z:
            found = [int(m.group(1)) for n in z.namelist() if (m := PLATE_RE.match(n))]
    except (zipfile.BadZipFile, OSError):
        return []
    return sorted(found)


def job_name(name: str) -> str:
    """A file name the printer's card takes: letters, digits, dot, dash, underscore, ending in .gcode.3mf."""
    stem = re.split(r"[\\/]", name)[-1]                    # no folders, however they were written
    for suffix in (".gcode.3mf", ".3mf"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "model"
    return f"{stem[:80]}{JOB_SUFFIX}"


def parse_mapping(text) -> list:
    """'0,1' or [0, 1] -> [0, 1]. Each is a slot as the printer numbers them (AMS 1 is 0-3, AMS 2 is 4-7...), -1 for a
    filament the job does not use, or 254 for the external spool."""
    if text is None or text == "":
        return []
    items = text if isinstance(text, (list, tuple)) else str(text).split(",")
    out = []
    for item in items:
        try:
            value = int(str(item).strip())
        except ValueError:
            raise PrinterError("The AMS mapping must be whole numbers, like 0,1")
        if value not in range(-1, 16) and value != 254:
            raise PrinterError("An AMS slot must be 0 to 15 (or 254 for the external spool, -1 for unused)")
        out.append(value)
    return out


def start_payload(remote_name: str, plate: int, use_ams: bool, ams_mapping: list, bed_type: str = "textured_plate") -> dict:
    """The `print` object of the project_file message, in the shape bambulabs-api 2.6.6 sends."""
    return {"print": {
        "command": "project_file", "param": f"Metadata/plate_{int(plate)}.gcode", "file": remote_name,
        "url": f"ftp:///{remote_name}", "bed_type": bed_type, "bed_leveling": True, "flow_cali": True,
        "vibration_cali": True, "layer_inspect": False, "sequence_id": "10000000",
        "use_ams": bool(use_ams), "ams_mapping": list(ams_mapping), "skip_objects": None}}


# ---------- the network parts (tests replace these two) ----------

class _ImplicitFTPS(ftplib.FTP_TLS):
    """FTP over TLS from the first byte (the printer's way), with the data connection reusing the control connection's TLS session."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tls_sock = None

    @property
    def sock(self):
        return self._tls_sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._tls_sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=32768, callback=None, rest=None):
        # ftplib's version unwraps TLS at the end, which these printers never answer. This one sends the end-of-data signal
        # (a FIN, no TLS close_notify), waits for the server's "226" and only then closes. Closing first can make the
        # operating system reset the connection while TLS messages from the server are still unread, which drops the tail.
        self.voidcmd("TYPE I")
        conn = self.transfercmd(cmd, rest)
        try:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
                if callback:
                    callback(buf)
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            return self.voidresp()
        finally:
            conn.close()


def _tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE        # the printer's certificate is self-signed; this is your own network
    return context


def _ftp_upload(host: str, code: str, path: Path, remote_name: str, port: int = FTP_PORT, timeout: float = FTP_TIMEOUT) -> None:
    ftp = _ImplicitFTPS(context=_tls_context(), timeout=timeout)
    try:
        ftp.connect(host, port, timeout=timeout)
        ftp.login("bblp", code)
        ftp.prot_p()
        with open(path, "rb") as handle:
            reply = ftp.storbinary(f"STOR {remote_name}", handle)
        if not str(reply).startswith("226"):
            raise PrinterError("The printer did not confirm the upload (is the microSD card in and not full?)")
    except ftplib.error_perm as e:
        if str(e).startswith("530"):
            raise PrinterError("The printer refused the access code")
        raise PrinterError("The printer refused the file (is the microSD card in, and is there room on it?)")
    except (OSError, EOFError, ftplib.Error, ssl.SSLError) as e:
        raise PrinterError(f"Could not upload to the printer ({e.__class__.__name__}); is LAN mode on and the address right?")
    finally:
        try:
            ftp.close()
        except Exception:
            pass


def _mqtt_start(host: str, serial: str, code: str, payload: dict, port: int = MQTT_PORT, timeout: float = MQTT_TIMEOUT) -> None:
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"modelhub-start-{serial[-6:]}")
    client.username_pw_set("bblp", code)
    client.tls_set_context(_tls_context())
    try:
        client.connect(host, port, keepalive=10)
        client.loop_start()
        info = client.publish(f"device/{serial}/request", json.dumps(payload), qos=1)
        info.wait_for_publish(timeout)
        if not info.is_published():
            raise PrinterError("The printer did not take the start command (is the access code right?)")
    except OSError as e:
        raise PrinterError(f"Could not reach the printer ({e.__class__.__name__})")
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


# ---------- the whole job ----------

def printer_state(host: str, serial: str, code: str) -> str:
    report = printers._bambu_report(host, serial, code)
    raw = str(report.get("gcode_state") or "").upper()
    return printers._BAMBU_STATES.get(raw, raw.lower() or "unknown")


def send_job(host: str, serial: str, code: str, path: Path, filename: str, start: bool, plate: Optional[int] = None,
             use_ams: bool = True, ams_mapping: Optional[list] = None, bed_type: str = "textured_plate") -> dict:
    """Upload a sliced .gcode.3mf and, when start is true, start the chosen plate. Returns {"filename", "started", "plate"}.
    Raises PrinterError for anything the printer or the file gets wrong, and never starts a print without start=True."""
    plates = plate_numbers(path)
    if not plates:
        raise PrinterError("That 3MF has no sliced G-code in it. In the slicer choose 'Export plate sliced file' (.gcode.3mf), not the plain project")
    plate = plates[0] if plate is None else plate
    if plate not in plates:
        raise PrinterError(f"Plate {plate} is not in that file (it has {', '.join(map(str, plates))})")
    if bed_type not in BED_TYPES:
        raise PrinterError(f"The bed type must be one of {', '.join(BED_TYPES)}")
    ams_mapping = list(ams_mapping or [])
    if start and use_ams and not ams_mapping:
        raise PrinterError("Choose which AMS slot each filament should come from, or turn the AMS off to use the external spool")
    size = path.stat().st_size
    if size == 0:
        raise PrinterError("That file is empty")
    if size > printers.MAX_UPLOAD_BYTES:
        raise PrinterError("That file is larger than 1 GB")
    remote_name = job_name(filename)

    state = printer_state(host, serial, code)
    if state in BUSY_STATES:
        raise PrinterError(f"The printer is busy ({state}); wait for it to finish before sending another job")

    _ftp_upload(host, code, path, remote_name)
    if not start:
        return {"filename": remote_name, "started": False, "plate": plate}

    _mqtt_start(host, serial, code, start_payload(remote_name, plate, use_ams, ams_mapping, bed_type))
    deadline = time.monotonic() + START_WAIT_SECONDS
    while True:
        time.sleep(START_POLL_SECONDS)
        try:
            if printer_state(host, serial, code) in BUSY_STATES:
                return {"filename": remote_name, "started": True, "plate": plate}
        except PrinterError:
            pass
        if time.monotonic() >= deadline:
            break
    raise PrinterError("The file is on the printer's card but it did not start. Check that LAN Only and Developer Mode are both on "
                       "(otherwise the printer ignores outside start commands), that the card is in, and that the printer is idle")
