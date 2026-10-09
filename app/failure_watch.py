"""Look at a printer's camera now and then and say when the print seems to have failed (spaghetti, a part that came loose).

Off for every printer until switched on, needs the printer's camera picture address and a LOCAL vision model (Ollama): a picture every
two minutes sent to a paid service would add up and leave your network. It only ever tells you; it never pauses or stops a printer.
A vision model can be wrong both ways, so a warning needs two checks in a row, and what it says is a hint, not a verdict."""
import json
import logging
from typing import Optional

from sqlmodel import Session, select

from app import printers as printing, printwatch
from app.models import Printer
from app.notify import notify_event

logger = logging.getLogger("modelhub.failure_watch")

PROMPT = ("You are watching a 3D printer's camera while it prints. Decide whether the print has FAILED right now: loose strings of filament "
          "(spaghetti), the part knocked off or lifted from the bed, a blob around the nozzle, or nothing on the bed although it is printing. "
          "A normal print in progress, even a half-finished or messy-looking one, is NOT a failure. "
          'Reply ONLY with JSON: {"failed": true or false, "reason": "a few words"}')
STRIKES_NEEDED = 2

_state: dict = {}          # printer id -> {"strikes": int, "told": bool, "last": {...}}


class WatchError(Exception):
    pass


def reset() -> None:
    _state.clear()


def verdict(session: Session, picture: bytes) -> dict:
    """Ask the local vision model about one picture: {"failed": bool, "reason": str}. (Tests replace this.)"""
    import base64
    from app.ai import get_provider
    from app.ai.http_utils import post_json_bounded
    from app.ai.ollama_provider import OllamaProvider
    provider = get_provider(session)
    if not isinstance(provider, OllamaProvider):
        raise WatchError("Watching needs a local vision model: set the AI mode to local (Ollama) in Settings")
    try:
        body = post_json_bounded(f"{provider.host}/api/generate", {"model": provider.vision_model, "prompt": PROMPT, "images": [base64.b64encode(picture).decode()],
                                                                   "stream": False, "format": "json", "options": {"num_predict": 120, "temperature": 0}}, timeout=120)
    except Exception as e:
        raise WatchError(f"The vision model did not answer ({e.__class__.__name__})")
    try:
        data = json.loads(body.get("response", "{}"))
    except ValueError:
        raise WatchError("The vision model answered with something unreadable")
    return {"failed": data.get("failed") is True, "reason": str(data.get("reason") or "")[:120]}


def check(session: Session, printer: Printer) -> dict:
    """One look at one printer. Raises WatchError when it cannot be done."""
    if not printer.snapshot_url:
        raise WatchError("Give the printer its camera picture address first")
    try:
        picture = printing.fetch_snapshot(printer.snapshot_url)
    except printing.PrinterError as e:
        raise WatchError(str(e))
    return verdict(session, picture)


def scheduled(session: Session) -> list:
    """Look at every watched printer that is printing. Returns the names of printers a warning was sent for."""
    warned = []
    for printer in session.exec(select(Printer).where(Printer.watch_failures.is_(True))).all():
        state = _state.setdefault(printer.id, {"strikes": 0, "told": False, "last": None})
        latest = printwatch.latest.get(printer.id) or {}
        if latest.get("state") != "printing":
            state.update(strikes=0, told=False)                  # a new print starts with a clean slate
            continue
        try:
            result = check(session, printer)
        except WatchError as e:
            state["last"] = {"error": str(e)}
            logger.info("Watching %s: %s", printer.name, e)
            continue
        state["last"] = result
        state["strikes"] = state["strikes"] + 1 if result["failed"] else 0
        if state["strikes"] >= STRIKES_NEEDED and not state["told"]:
            state["told"] = True
            notify_event(session, "failure_suspected", f"Model Hub: {printer.name} may have a failed print",
                         f"The camera shows what looks like a failure ({result['reason'] or 'no reason given'}). {latest.get('file') or 'The print'} is at {latest.get('progress')}%. Please take a look.")
            warned.append(printer.name)
    return warned
