"""Publishing printer state and library numbers to an MQTT broker, with Home Assistant discovery.

Off unless an MQTT host is set in Settings. Model Hub only ever publishes (it subscribes to nothing and cannot be
controlled this way). State is retained so a restarted Home Assistant sees the last values. Printer state comes from the
printer poll that is already running; nothing extra is asked of your printers.

Topics (prefix defaults to "modelhub"):
  <prefix>/printer/<id>/state   JSON {name, online, state, progress, file, nozzle, bed}
  <prefix>/stats                JSON {models, never_printed, prints_this_month, low_stock, filament_remaining_g, queue_waiting, update_available}
Home Assistant discovery messages go to homeassistant/... when discovery is on.
"""
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app import print_outcomes, printwatch, stock_watch, update_check
from app.config import MODEL_EXTENSIONS
from app.models import Model3D, Printer, PrintLog, QueueItem
from app.settings_store import get_setting, set_setting

logger = logging.getLogger("modelhub.mqtt")

DISCOVERY_EVERY = 3600
_discovery = {"at": 0.0, "key": ""}


class MqttError(Exception):
    pass


def settings(session: Session) -> Optional[dict]:
    """The connection settings, or None when MQTT is off (no host)."""
    host = (get_setting(session, "mqtt_host", "") or "").strip()
    if not host:
        return None
    try:
        port = int(get_setting(session, "mqtt_port", "") or 1883)
    except ValueError:
        port = 1883
    prefix = re.sub(r"[^A-Za-z0-9_/-]", "", (get_setting(session, "mqtt_prefix", "") or "modelhub").strip("/")) or "modelhub"
    user = (get_setting(session, "mqtt_user", "") or "").strip()
    password = get_setting(session, "mqtt_password", "") or ""
    return {"host": host, "port": port if 0 < port < 65536 else 1883, "prefix": prefix,
            "auth": {"username": user, "password": password} if user else None,
            "tls": (get_setting(session, "mqtt_tls", "") or "false") == "true",
            "discovery": (get_setting(session, "mqtt_discovery", "") or "true") != "false"}


def _send(messages: list, cfg: dict) -> None:
    """Publish [(topic, payload, retain)] over one connection. (Patched in tests.)"""
    import paho.mqtt.publish as publish
    publish.multiple([{"topic": t, "payload": p, "retain": r, "qos": 0} for t, p, r in messages],
                     hostname=cfg["host"], port=cfg["port"], auth=cfg["auth"], tls={} if cfg["tls"] else None,
                     client_id="modelhub", keepalive=30)


def stats_payload(session: Session) -> dict:
    month = datetime.utcnow().strftime("%Y-%m")
    total = session.exec(select(func.count()).select_from(Model3D).where(Model3D.extension.in_(MODEL_EXTENSIONS))).one()
    printed = session.exec(select(func.count(func.distinct(PrintLog.model_id))).where(print_outcomes.ok())).one()
    this_month = sum(1 for at in session.exec(select(PrintLog.printed_at).where(print_outcomes.ok())).all() if at.strftime("%Y-%m") == month)
    from app.models import Filament
    return {"models": total, "never_printed": max(0, total - printed), "prints_this_month": this_month,
            "low_stock": len(stock_watch.current_low(session)),
            "filament_remaining_g": round(sum(f.remaining_g for f in session.exec(select(Filament)).all()), 1),
            "queue_waiting": len(session.exec(select(QueueItem.id).where(QueueItem.status == "queued")).all()),
            "update_available": bool(update_check.status(session)["update_available"])}


def _device(name: str, key: str) -> dict:
    return {"identifiers": [key], "name": name, "manufacturer": "Model Hub"}


def discovery_messages(prefix: str, printers: list) -> list:
    """Home Assistant MQTT discovery configs (retained)."""
    out = []
    for p in printers:
        key = f"modelhub_printer_{p.id}"
        base = f"{prefix}/printer/{p.id}/state"
        device = _device(p.name, key)
        for field, label, extra in (("state", "State", {}), ("progress", "Progress", {"unit_of_measurement": "%"}),
                                    ("nozzle", "Nozzle temperature", {"unit_of_measurement": "\u00b0C", "device_class": "temperature"}),
                                    ("bed", "Bed temperature", {"unit_of_measurement": "\u00b0C", "device_class": "temperature"}),
                                    ("file", "File", {})):
            out.append((f"homeassistant/sensor/{key}/{field}/config", json.dumps({
                "name": f"{p.name} {label}".strip(), "unique_id": f"{key}_{field}", "state_topic": base,
                "value_template": "{{ value_json.%s }}" % field, "device": device, **extra}), True))
        out.append((f"homeassistant/binary_sensor/{key}/online/config", json.dumps({
            "name": f"{p.name} online", "unique_id": f"{key}_online", "state_topic": base, "value_template": "{{ value_json.online }}",
            "payload_on": "True", "payload_off": "False", "device_class": "connectivity", "device": device}), True))
    device = _device("Model Hub", "modelhub_library")
    for field, label, unit in (("models", "Models", None), ("never_printed", "Never printed", None), ("prints_this_month", "Prints this month", None),
                               ("low_stock", "Low stock items", None), ("filament_remaining_g", "Filament remaining", "g"), ("queue_waiting", "Queued jobs", None)):
        cfg = {"name": f"Model Hub {label}", "unique_id": f"modelhub_library_{field}", "state_topic": f"{prefix}/stats",
               "value_template": "{{ value_json.%s }}" % field, "device": device}
        if unit:
            cfg["unit_of_measurement"] = unit
        out.append((f"homeassistant/sensor/modelhub_library/{field}/config", json.dumps(cfg), True))
    out.append(("homeassistant/binary_sensor/modelhub_library/update_available/config", json.dumps({
        "name": "Model Hub update available", "unique_id": "modelhub_library_update", "state_topic": f"{prefix}/stats",
        "value_template": "{{ value_json.update_available }}", "payload_on": "True", "payload_off": "False", "device_class": "update", "device": device}), True))
    return out


def messages(session: Session, cfg: dict, include_discovery: bool) -> list:
    prefix = cfg["prefix"]
    printers = session.exec(select(Printer).order_by(Printer.id)).all()
    out = []
    for p in printers:
        st = printwatch.latest.get(p.id)
        if not st:
            continue
        out.append((f"{prefix}/printer/{p.id}/state", json.dumps({
            "name": p.name, "online": bool(st["online"]), "state": st["state"], "progress": st["progress"], "file": st["file"],
            "nozzle": st["nozzle"], "bed": st["bed"]}), True))
    out.append((f"{prefix}/stats", json.dumps(stats_payload(session)), True))
    if include_discovery and cfg["discovery"]:
        out = discovery_messages(prefix, printers) + out
    return out


def publish(session: Session) -> int:
    """One publish run. Returns how many messages went out (0 when MQTT is off). Raises MqttError on a failure."""
    cfg = settings(session)
    if not cfg:
        return 0
    printers = session.exec(select(Printer.id)).all()
    key = ",".join(str(i) for i in printers) + f"|{cfg['prefix']}"
    now = time.time()
    due = key != _discovery["key"] or now - _discovery["at"] > DISCOVERY_EVERY
    out = messages(session, cfg, include_discovery=due)
    try:
        _send(out, cfg)
    except Exception as e:                       # a broker that is down, a wrong password, a bad address...
        set_setting(session, "mqtt_last_error", f"{e.__class__.__name__}: {str(e)[:150]}")
        raise MqttError(f"Could not publish to the MQTT broker ({e.__class__.__name__})")
    if due:
        _discovery.update(at=now, key=key)
    if get_setting(session, "mqtt_last_error", ""):
        set_setting(session, "mqtt_last_error", "")
    return len(out)


def scheduled(session: Session) -> None:
    try:
        publish(session)
    except MqttError as e:
        logger.info("%s", e)


def send_test(session: Session) -> str:
    cfg = settings(session)
    if not cfg:
        raise MqttError("Enter the MQTT broker's address first")
    try:
        _send([(f"{cfg['prefix']}/test", json.dumps({"message": "Model Hub test", "at": datetime.utcnow().isoformat() + "Z"}), False)], cfg)
    except Exception as e:
        raise MqttError(f"Could not reach the broker ({e.__class__.__name__}: {str(e)[:120]})")
    return f"Sent a test message to {cfg['prefix']}/test"


def reset() -> None:
    _discovery.update(at=0.0, key="")
