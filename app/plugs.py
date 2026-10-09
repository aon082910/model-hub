"""Measure what a print used from a smart plug that counts energy (Tasmota, Shelly).

Only the plug's running energy total is read, at the start and at the end of a print; the difference is what the print used. If the plug
cannot be reached the print is recorded without a figure: this never gets in the way of recording a print."""
import logging
from typing import Optional

from app import printers as printing

logger = logging.getLogger("modelhub.plugs")
KINDS = ("tasmota", "shelly")
MAX_KWH_PER_PRINT = 500.0


class PlugError(Exception):
    pass


def _get(host: str, path: str):
    try:
        with printing._client(timeout=printing.TIMEOUT) as client:
            response = client.get(f"http://{host}{path}")
    except Exception as e:
        raise PlugError(f"The plug did not answer ({e.__class__.__name__})")
    if response.status_code != 200:
        raise PlugError(f"The plug answered with an error ({response.status_code})")
    try:
        return response.json()
    except ValueError:
        raise PlugError("The plug answered with something unreadable")


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise PlugError("The plug did not report an energy total")
    return float(value)


def read_total_kwh(kind: str, host: str) -> float:
    """The plug's running energy total in kWh."""
    if kind == "tasmota":
        data = _get(host, "/cm?cmnd=Status%208")
        energy = ((data.get("StatusSNS") or {}).get("ENERGY") or {}) if isinstance(data, dict) else {}
        return _number(energy.get("Total"))
    if kind == "shelly":
        try:
            data = _get(host, "/rpc/Switch.GetStatus?id=0")            # second generation: watt-hours
            return _number((data.get("aenergy") or {}).get("total")) / 1000
        except PlugError:
            data = _get(host, "/status")                               # first generation: watt-minutes
            meters = data.get("meters") if isinstance(data, dict) else None
            if not isinstance(meters, list) or not meters or not isinstance(meters[0], dict):
                raise PlugError("The plug did not report an energy total")
            return _number(meters[0].get("total")) / 60000
    raise PlugError("Choose Tasmota or Shelly")


def try_total(kind: Optional[str], host: Optional[str]) -> Optional[float]:
    if kind not in KINDS or not host:
        return None
    try:
        return read_total_kwh(kind, host)
    except PlugError as e:
        logger.info("No energy reading from the plug at %s: %s", host, e)
        return None


def used(start: Optional[float], end: Optional[float]) -> Optional[float]:
    """kWh between two readings, or None when either is missing or the counter went backwards (a plug that was reset)."""
    if start is None or end is None or end < start or end - start > MAX_KWH_PER_PRINT:
        return None
    return round(end - start, 3)
