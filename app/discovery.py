"""Finding printers on your network, working out why one cannot be reached, and a support bundle.

* **Scan.** You give a network range (like 192.168.1.0/24, at most 256 addresses, private ranges only: Model Hub never scans the internet). Each address is asked, briefly, whether it is a Klipper
  (Moonraker, port 7125) or an OctoPrint (port 80 or 5000) printer. A Bambu Lab printer has no open question to ask without its access code, so an address with both its ports (8883 and 990) open is
  listed as *probably a Bambu Lab printer*; add it with its serial number and access code. Nothing is changed by a scan.
* **Diagnose.** For a printer already added: does the address resolve, is the port open, does it answer, does it accept the key, does the camera give a picture, does the smart plug answer.
* **Support bundle.** A zip with the version, a count of what is stored, which settings are set (names only, never values), the printers (without addresses' credentials or keys) and the
  recent log, for asking for help without sending secrets."""
import ipaddress
import json
import socket
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import func
from sqlmodel import Session, select

from app import printers as printing
from app.models import Printer

MAX_HOSTS = 256
PROBE_TIMEOUT = 0.9
WORKERS = 48


class ScanError(Exception):
    """A range that cannot be scanned; the message is safe to show."""


def parse_range(text: str) -> list:
    """The addresses of a private network range, or ScanError."""
    try:
        net = ipaddress.ip_network((text or "").strip(), strict=False)
    except ValueError:
        raise ScanError("Give a network range like 192.168.1.0/24")
    if net.version != 4:
        raise ScanError("Only IPv4 ranges are scanned")
    if not (net.is_private and not net.is_loopback and not net.is_link_local and not net.is_multicast):
        raise ScanError("Only private network ranges (like 192.168.x.x, 10.x.x.x or 172.16-31.x.x) are scanned")
    if net.num_addresses > MAX_HOSTS:
        raise ScanError(f"A range of at most {MAX_HOSTS} addresses (a /24) at a time")
    return [str(h) for h in net.hosts()] if net.num_addresses > 2 else [str(a) for a in net]


def _tcp_open(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _get_json(url: str):
    try:
        r = httpx.get(url, timeout=PROBE_TIMEOUT + 0.8, follow_redirects=False)
        return r.status_code, (r.json() if r.status_code == 200 else None)
    except Exception:
        return None, None


def probe(ip: str) -> Optional[dict]:
    """What printer, if any, answers at this address. (Tests replace this.)"""
    if _tcp_open(ip, 7125):
        status, data = _get_json(f"http://{ip}:7125/server/info")
        if status == 200 and isinstance(data, dict) and "result" in data:
            name = None
            s2, info = _get_json(f"http://{ip}:7125/printer/info")
            if s2 == 200 and isinstance(info, dict):
                name = ((info.get("result") or {}).get("hostname")) or None
            return {"ip": ip, "kind": "moonraker", "url": f"http://{ip}:7125", "name": name or ip, "note": "Klipper (Moonraker)"}
        if status in (401, 403):
            return {"ip": ip, "kind": "moonraker", "url": f"http://{ip}:7125", "name": ip, "note": "Klipper (Moonraker), asks for an API key"}
    for port in (80, 5000):
        if _tcp_open(ip, port):
            status, data = _get_json(f"http://{ip}:{port}/api/version")
            if status == 200 and isinstance(data, dict) and "OctoPrint" in json.dumps(data):
                return {"ip": ip, "kind": "octoprint", "url": f"http://{ip}" + ("" if port == 80 else f":{port}"), "name": ip, "note": "OctoPrint"}
            if status in (401, 403):
                return {"ip": ip, "kind": "octoprint", "url": f"http://{ip}" + ("" if port == 80 else f":{port}"), "name": ip, "note": "OctoPrint (or similar), asks for an API key"}
    if _tcp_open(ip, 8883) and _tcp_open(ip, 990):
        return {"ip": ip, "kind": "bambu", "url": ip, "name": ip, "note": "Probably a Bambu Lab printer: add it with its serial number and access code"}
    return None


def scan(session: Session, text: str) -> dict:
    hosts = parse_range(text)
    known = set()
    for p in session.exec(select(Printer)).all():
        host = urlparse(p.url if "://" in p.url else "http://" + p.url).hostname
        if host:
            known.add(host)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        found = [r for r in pool.map(probe, hosts) if r]
    for r in found:
        r["added"] = r["ip"] in known
    return {"scanned": len(hosts), "found": found}


# ---------------------------------------------------------------- diagnose
def _step(name: str, ok: bool, detail: str) -> dict:
    return {"step": name, "ok": ok, "detail": detail}


def diagnose(printer: Printer) -> dict:
    steps = []
    host = urlparse(printer.url if "://" in printer.url else "http://" + printer.url).hostname or printer.url
    port = 8883 if printer.kind == "bambu" else (urlparse(printer.url).port or (443 if printer.url.startswith("https") else 80))
    try:
        ip = socket.gethostbyname(host)
        steps.append(_step("Address", True, f"{host} is {ip}"))
    except OSError:
        steps.append(_step("Address", False, f"The name {host} does not resolve to an address. Check it, or use the IP address"))
        return {"steps": steps, "summary": "The address does not resolve."}
    if _tcp_open(ip, port, 3.0):
        steps.append(_step("Port", True, f"Port {port} is open"))
    else:
        steps.append(_step("Port", False, f"Nothing answers on port {port}. Is the printer on, on the same network (not blocked by a firewall or a guest network), and the port right?"))
        return {"steps": steps, "summary": "The printer cannot be reached on that port."}
    st = printing.status(printer.kind, printer.url, printer.api_key, printer.serial)
    if st["online"]:
        steps.append(_step("Answer", True, f"It answers: {st['state']}" + (f", nozzle {st['nozzle']} °C" if st.get("nozzle") is not None else "")))
    else:
        message = st.get("message") or "no answer"
        steps.append(_step("Answer", False, message + (". The API key is probably missing or wrong" if "401" in message or "403" in message else "")))
    if printer.snapshot_url:
        try:
            size = len(printing.fetch_snapshot(printer.snapshot_url))
            steps.append(_step("Camera", True, f"A picture came back ({size // 1024} KB)"))
        except Exception as e:
            steps.append(_step("Camera", False, str(e) if isinstance(e, printing.PrinterError) else f"The camera failed ({e.__class__.__name__})"))
    if printer.plug_kind:
        from app import plugs
        total = plugs.try_total(printer.plug_kind, printer.plug_host)
        steps.append(_step("Smart plug", total is not None, f"Energy total {total} kWh" if total is not None else "The plug did not answer"))
    bad = [s for s in steps if not s["ok"]]
    return {"steps": steps, "summary": "Everything checked answers." if not bad else bad[0]["detail"]}


# ---------------------------------------------------------------- support bundle
def support_bundle(session: Session) -> bytes:
    import platform
    import sys
    from sqlmodel import SQLModel
    from app import logbuffer, scheduler, version
    from app.models import AppSettings
    counts = {}
    for name, table in SQLModel.metadata.tables.items():
        if name in ("appsettings", "appuser", "apitoken", "sharelink", "usersecurity"):
            continue
        try:
            counts[name] = session.exec(select(func.count()).select_from(table)).one()
        except Exception:
            pass
    keys = sorted(r.key for r in session.exec(select(AppSettings)).all())
    printers = [{"id": p.id, "name": p.name, "kind": p.kind, "host": urlparse(p.url if "://" in p.url else "http://" + p.url).hostname, "has_key": bool(p.api_key), "camera": bool(p.snapshot_url),
                 "out_of_service": bool(p.out_of_service), "plug": p.plug_kind} for p in session.exec(select(Printer)).all()]
    info = {"made": datetime.utcnow().isoformat() + "Z", "version": version.VERSION, "python": sys.version.split()[0], "platform": platform.platform(),
            "tables": counts, "settings_that_are_set": keys, "printers": printers, "scheduler_last_run": {k: round(v) for k, v in scheduler._last_run.items()},
            "note": "Setting values, keys, passwords, tokens and secret links are not included."}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.json", json.dumps(info, indent=1, default=str))
        z.writestr("log.txt", logbuffer.text("INFO"))
    return out.getvalue()
