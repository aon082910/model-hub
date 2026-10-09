"""A print finished: is the plate cleared? An optional gate between prints.

With the gate on (Settings, Print planning), a printer that finished or stopped a print is *waiting for its plate to be cleared* until someone says it is (a button, or the
API call `POST /api/printers/<id>/plate-cleared`, which Home Assistant or a script can make), or, if the printer has a camera and a local vision model, until the camera sees an
empty plate twice in a row. While it waits, sending a print to it asks first (you can always start anyway), and the state is published over MQTT. A new print starting clears it.
The camera never decides anything alone about a print: it only says the plate looks empty."""
import json
import logging
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app import printers as printing
from app.models import Printer
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

logger = logging.getLogger("modelhub.plate")

PROMPT = ("You are looking at the print bed of a 3D printer after a print has finished. Is there ANY printed object, part of a print, "
          "stringy filament or other debris left on the bed? A clean, empty bed (even with a visible bed texture or a small purge line at the edge) is NOT occupied. "
          'Reply ONLY with JSON: {"occupied": true or false}')
EMPTY_LOOKS_NEEDED = 2
_empty_looks: dict = {}                 # printer id -> consecutive looks at an empty plate


def gate_enabled(session: Session) -> bool:
    return get_setting(session, "plate_clear_gate", "") == "true"


def awaiting(session: Session) -> dict:
    try:
        data = json.loads(get_setting(session, "plate_awaiting", "{}") or "{}")
    except ValueError:
        return {}
    return {int(k): v for k, v in data.items() if str(k).isdigit()} if isinstance(data, dict) else {}


def _save(session: Session, data: dict) -> None:
    set_setting(session, "plate_awaiting", json.dumps({str(k): v for k, v in data.items()}))


def is_awaiting(session: Session, printer_id: int) -> bool:
    return gate_enabled(session) and printer_id in awaiting(session)


def mark_awaiting(session: Session, printer: Printer) -> bool:
    """A print ended on this printer. True if it now waits for its plate (the gate is on)."""
    if not gate_enabled(session):
        return False
    data = awaiting(session)
    if printer.id not in data:
        data[printer.id] = datetime.utcnow().isoformat()
        _save(session, data)
        notify_event(session, "plate_clear", f"Model Hub: {printer.name} needs its plate cleared", f"{printer.name} finished a print. Clear the plate, then say so in Model Hub.",
                     fields={"printer": printer.name, "file": "", "progress": "", "minutes": ""})
    _empty_looks.pop(printer.id, None)
    return True


def clear(session: Session, printer_id: int) -> bool:
    """The plate is clear (said by hand, by the camera, or because the next print started). True if it was waiting."""
    data = awaiting(session)
    _empty_looks.pop(printer_id, None)
    if printer_id in data:
        data.pop(printer_id)
        _save(session, data)
        return True
    return False


def forget_printer(session: Session, printer_id: int) -> None:
    clear(session, printer_id)


def reason_to_wait(session: Session, printer: Printer) -> Optional[str]:
    if is_awaiting(session, printer.id):
        return f"{printer.name} has not had its plate cleared since its last print finished"
    return None


def verdict(session: Session, picture: bytes) -> dict:
    """Ask the local vision model whether anything is left on the bed: {"occupied": bool}. (Tests replace this.)"""
    import base64
    from app.ai import get_provider
    from app.ai.http_utils import post_json_bounded
    from app.ai.ollama_provider import OllamaProvider
    provider = get_provider(session)
    if not isinstance(provider, OllamaProvider):
        raise printing.PrinterError("The plate check needs a local vision model: set the AI mode to local (Ollama) in Settings")
    try:
        body = post_json_bounded(f"{provider.host}/api/generate", {"model": provider.vision_model, "prompt": PROMPT, "images": [base64.b64encode(picture).decode()],
                                                                   "stream": False, "format": "json", "options": {"num_predict": 60, "temperature": 0}}, timeout=120)
        data = json.loads(body.get("response", "{}"))
    except printing.PrinterError:
        raise
    except Exception as e:
        raise printing.PrinterError(f"The vision model did not answer ({e.__class__.__name__})")
    return {"occupied": data.get("occupied") is not False}          # anything but a clear "no" counts as occupied


def look(session: Session, printer: Printer) -> dict:
    if not printer.snapshot_url:
        raise printing.PrinterError("Give the printer its camera picture address first")
    return verdict(session, printing.fetch_snapshot(printer.snapshot_url))


def scheduled(session: Session) -> list:
    """For printers waiting for their plate and set to be checked by camera: clear those the camera sees empty twice in a row. Returns the names cleared."""
    if not gate_enabled(session):
        return []
    cleared = []
    for printer_id in list(awaiting(session)):
        printer = session.get(Printer, printer_id)
        if not printer or not printer.plate_check:
            continue
        try:
            result = look(session, printer)
        except printing.PrinterError as e:
            logger.info("Plate check of %s: %s", printer.name, e)
            continue
        _empty_looks[printer_id] = 0 if result["occupied"] else _empty_looks.get(printer_id, 0) + 1
        if _empty_looks[printer_id] >= EMPTY_LOOKS_NEEDED:
            clear(session, printer_id)
            cleared.append(printer.name)
            from app import activity
            activity.record(session, "the camera", "printer", f"{printer.name}: the plate looks empty, so it was marked as cleared")
    return cleared
