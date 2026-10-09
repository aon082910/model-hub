"""Hold a printer's queued prints while a Home Assistant sensor is alerting (a door that is open, a chamber that is too hot).

Model Hub asks Home Assistant for the sensor's state every half minute (with the long-lived token you put in Settings; it is kept out of backups). While the sensor
is alerting, the printer is not suggested for new prints and sending to it waits, with the reason shown; you can still start anyway.
If the sensor cannot be read (Home Assistant is down, the sensor is unavailable) nothing is held: a broken sensor must not stop the farm."""
import re
import time
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlmodel import Session, select

from app.models import PrinterSensor
from app.settings_store import get_setting

ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
CONDITIONS = ("on", "off", "above", "below")
_state: dict = {}            # sensor id -> {"alerting": bool or None, "value": str, "error": str, "at": time}


class SensorError(Exception):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=8, follow_redirects=False, headers={"User-Agent": "ModelHub/sensors"})


def reset() -> None:
    _state.clear()


def clean_url(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SensorError("Give Home Assistant's address in Settings first (like http://192.168.1.5:8123)")
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SensorError("Home Assistant's address must look like http://192.168.1.5:8123")
    return value.strip().rstrip("/")


def read_state(session: Session, entity_id: str) -> str:
    if not ENTITY.match(entity_id or ""):
        raise SensorError("A sensor name looks like binary_sensor.printer_door")
    base = clean_url(get_setting(session, "ha_url", ""))
    token = get_setting(session, "ha_token", "")
    if not token:
        raise SensorError("Put a long-lived access token for Home Assistant in Settings first")
    try:
        with _client() as client:
            response = client.get(f"{base}/api/states/{entity_id}", headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        raise SensorError(f"Home Assistant did not answer ({e.__class__.__name__})")
    if response.status_code in (401, 403):
        raise SensorError("Home Assistant refused the token")
    if response.status_code == 404:
        raise SensorError("Home Assistant has no sensor with that name")
    if response.status_code != 200:
        raise SensorError(f"Home Assistant answered with an error ({response.status_code})")
    try:
        return str(response.json().get("state"))
    except (ValueError, AttributeError):
        raise SensorError("Home Assistant answered with something unreadable")


def evaluate(condition: str, threshold: Optional[float], value: str) -> Optional[bool]:
    """Is the sensor alerting? None when it cannot tell (unavailable, unknown, not a number)."""
    if value in ("unavailable", "unknown", "None", ""):
        return None
    if condition in ("on", "off"):
        return value == condition
    try:
        number = float(value)
    except ValueError:
        return None
    if threshold is None:
        return None
    return number > threshold if condition == "above" else number < threshold


def poll(session: Session) -> list:
    """Read every bound sensor once. Returns the names of printers that are on hold."""
    held = set()
    for sensor in session.exec(select(PrinterSensor)).all():
        try:
            value = read_state(session, sensor.entity_id)
            alerting = evaluate(sensor.condition, sensor.threshold, value)
            _state[sensor.id] = {"alerting": alerting, "value": value, "error": None, "at": time.time()}
            if alerting:
                held.add(sensor.printer_id)
        except SensorError as e:
            _state[sensor.id] = {"alerting": None, "value": None, "error": str(e), "at": time.time()}
    return sorted(held)


def hold_reason(session: Session, printer_id: int) -> Optional[str]:
    for sensor in session.exec(select(PrinterSensor).where(PrinterSensor.printer_id == printer_id)).all():
        st = _state.get(sensor.id)
        if st and st["alerting"]:
            return f"{sensor.label or sensor.entity_id} is {st['value']}"
    return None


def holds(session: Session) -> dict:
    out = {}
    for sensor in session.exec(select(PrinterSensor)).all():
        st = _state.get(sensor.id)
        if st and st["alerting"] and sensor.printer_id not in out:
            out[sensor.printer_id] = f"{sensor.label or sensor.entity_id} is {st['value']}"
    return out


def forget_printer(session: Session, printer_id: int) -> None:
    for sensor in session.exec(select(PrinterSensor).where(PrinterSensor.printer_id == printer_id)).all():
        _state.pop(sensor.id, None)
        session.delete(sensor)
