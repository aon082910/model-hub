"""Notice when a printer finishes (or stops) a print.

Every poll looks at each printer's state. When one that was printing is not any more:
  * finished: if the file was one Model Hub sent for a model, the print is recorded for it (the
    matching print-queue entry is completed, which takes the filament off the spool; otherwise a
    print log entry with the print time is written), and you are notified.
  * stopped (cancelled or failed): you are notified, and a failed entry goes in the print log (the queue entry, if there was
    one, is marked failed). Say why in the log or the Stats page. A failed print never counts as the model having been printed.
Nothing is ever started or controlled by this. State is kept in memory only, so a print that
ends while Model Hub was restarting is not noticed.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from app import plugs, printers as printing
from app.models import Model3D, PrintLog, Printer, PrinterJob, PrintFile, QueueItem
from app.notify import event_enabled, is_discord, notify_event
from app.settings_store import get_setting

logger = logging.getLogger("modelhub.printwatch")

_last: dict = {}          # printer id -> {"state", "file"}
latest: dict = {}         # printer id -> the full status from the most recent poll
_energy_start: dict = {}  # printer id -> the plug's energy total when the print began
_progress_sent: dict = {} # printer id -> the last progress step announced


def reset() -> None:
    _last.clear()
    latest.clear()
    _energy_start.clear()
    _progress_sent.clear()


def finished(kind: str, state: str, progress) -> bool:
    """Did the print that just ended complete? (Moonraker says so; OctoPrint is judged by its progress.)"""
    if kind in ("moonraker", "bambu"):
        return state == "complete"
    return isinstance(progress, (int, float)) and progress >= 99.5


def poll(session: Session) -> list:
    """Look at every printer once. Returns [(printer name, outcome)] for prints that ended."""
    ended = []
    printers = session.exec(select(Printer)).all()
    for printer in printers:
        st = printing.status(printer.kind, printer.url, printer.api_key, printer.serial)
        latest[printer.id] = {**st, "name": printer.name}
        previous = _last.get(printer.id)
        if st["online"]:
            _announce(session, printer, previous, st)
            progress = st["progress"] if st["state"] == "printing" and st["progress"] is not None else (previous or {}).get("progress")
            _last[printer.id] = {"state": st["state"], "file": st["file"] or (previous or {}).get("file"), "progress": progress}
            if st["state"] == "printing" and (not previous or previous["state"] != "printing") and printer.plug_kind:
                reading = plugs.try_total(printer.plug_kind, printer.plug_host)
                if reading is not None:
                    _energy_start[printer.id] = reading
                else:
                    _energy_start.pop(printer.id, None)
        if not st["online"] or not previous or previous["state"] != "printing" or st["state"] == "printing":
            continue
        outcome = "done" if finished(printer.kind, st["state"], st["progress"]) else "stopped"
        kwh = None
        if printer.id in _energy_start:
            kwh = plugs.used(_energy_start.pop(printer.id), plugs.try_total(printer.plug_kind, printer.plug_host))
        _record(session, printer, previous.get("file"), outcome, st.get("duration"), progress=previous.get("progress"), energy_kwh=kwh)
        ended.append((printer.name, outcome))
    try:
        from app import maintenance
        maintenance.apply_hms(session)
    except Exception as e:                       # a mapping problem must never stop the poll
        logger.info("HMS check failed: %s", e.__class__.__name__)
    for gone in (set(_last) | set(latest)) - {p.id for p in printers}:
        _last.pop(gone, None)
        latest.pop(gone, None)
    return ended


def _picture(session: Session, printer: Printer, event: str) -> Optional[bytes]:
    """A camera picture to attach to a notification, when the event is on, pictures are on and the printer has a camera. Never raises."""
    if not printer.snapshot_url or get_setting(session, "notify_snapshots", "true") == "false" or not event_enabled(session, event) or not is_discord(get_setting(session, "notify_webhook_url", "")):
        return None
    try:
        return printing.fetch_snapshot(printer.snapshot_url)
    except Exception as e:
        logger.info("No picture for the notification from %s: %s", printer.name, e.__class__.__name__)
        return None


def _say(session: Session, printer: Printer, event: str, title: str, message: str, fields: dict) -> None:
    if event_enabled(session, event):
        notify_event(session, event, title, message, fields=fields, image=_picture(session, printer, event))


def _announce(session: Session, printer: Printer, previous, st: dict) -> None:
    """Say when a print starts, is paused or resumed, or passes another step of its progress (all off until you switch them on)."""
    state, was = st["state"], (previous or {}).get("state")
    progress = st.get("progress")
    fields = {"printer": printer.name, "file": st.get("file") or "a print", "progress": f"{round(progress)}%" if isinstance(progress, (int, float)) else "", "minutes": ""}
    if previous is not None:
        if state == "printing" and was not in ("printing", "paused"):
            _progress_sent[printer.id] = 0
            _say(session, printer, "print_started", f"Model Hub: {printer.name} started", f"{fields['file']} started.", fields)
        elif state == "paused" and was == "printing":
            _say(session, printer, "print_paused", f"Model Hub: {printer.name} paused", f"{fields['file']} is paused at {fields['progress'] or 'an unknown point'}.", fields)
        elif state == "printing" and was == "paused":
            _say(session, printer, "print_paused", f"Model Hub: {printer.name} resumed", f"{fields['file']} is printing again.", fields)
    if state not in ("printing", "paused"):
        _progress_sent.pop(printer.id, None)
        return
    try:
        step = min(50, max(1, int(float(get_setting(session, "notify_progress_step", "10") or 10))))
    except ValueError:
        step = 10
    if state == "printing" and isinstance(progress, (int, float)) and not isinstance(progress, bool):
        bucket = int(progress // step)
        last = _progress_sent.get(printer.id)
        _progress_sent[printer.id] = bucket if last is None else max(last, bucket)
        if last is not None and bucket > last and bucket * step < 100:
            _say(session, printer, "print_progress", f"Model Hub: {printer.name} is at {bucket * step}%", f"{fields['file']} is {bucket * step}% done.", fields)


def _photograph(session: Session, printer: Printer, model_id: int) -> None:
    """If this printer has a camera address, keep a picture of the finished print as the new log entry's photo.
    A camera that is off or wrong never gets in the way of recording the print."""
    if not printer.snapshot_url:
        return
    from app.routers.prints import photo_path, store_photo
    log = session.exec(select(PrintLog).where(PrintLog.model_id == model_id).order_by(PrintLog.id.desc())).first()
    if not log or (datetime.utcnow() - log.created_at).total_seconds() > 120 or photo_path(log.id).is_file():
        return
    try:
        store_photo(log.id, printing.fetch_snapshot(printer.snapshot_url))
    except Exception as e:                       # PrinterError, a picture that is not one, a full disk...
        logger.info("No photo of the finished print from %s: %s", printer.name, e.__class__.__name__)


def _record(session: Session, printer: Printer, filename, outcome: str, duration, progress=None, energy_kwh=None) -> None:
    job = None
    if filename:
        name = Path(str(filename)).name
        job = session.exec(select(PrinterJob).where(PrinterJob.printer_id == printer.id, PrinterJob.filename == name,
                                                    PrinterJob.finished_at.is_(None)).order_by(PrinterJob.id.desc())).first()
    label = filename or "a print"
    model = session.get(Model3D, job.model_id) if job and job.model_id else None
    if job:
        job.finished_at, job.outcome = datetime.utcnow(), outcome
        session.add(job)
    if model and outcome in ("done", "stopped"):
        from app.routers.prints import log_print
        from app.routers.queue import complete_item, fail_item
        minutes = duration / 60 if duration else None
        candidates = session.exec(select(QueueItem).where(QueueItem.model_id == model.id, QueueItem.status.in_(["queued", "printing"]))
                                  .order_by(QueueItem.position)).all()
        # the entry meant for this printer first (a model may be queued for several), else the next in line
        waiting = next((c for c in candidates if c.printer_id == printer.id), None) or (candidates[0] if candidates else None)
        if outcome == "done":
            if waiting:
                complete_item(session, waiting, minutes, printer.id)
            else:
                kept = session.get(PrintFile, job.print_file_id) if job and job.print_file_id else None
                log_print(session, model.id, minutes=minutes, grams=kept.est_grams if kept else None, source="printer", deduct=False,
                          measured=True, printer_id=printer.id)
            session.flush()
            _photograph(session, printer, model.id)
        elif waiting:
            fail_item(session, waiting, minutes=minutes, printer_id=printer.id, progress=progress)        # stopped or failed: the log keeps it, with the reason still to be said
        else:
            log_print(session, model.id, minutes=round(minutes, 1) if minutes else None, source="printer", deduct=False, outcome="failed",
                      printer_id=printer.id)
        if outcome == "stopped":
            session.flush()
            _photograph(session, printer, model.id)             # a picture of what went wrong is worth keeping too
        label = model.filename
        if energy_kwh is not None:
            session.flush()
            log = session.exec(select(PrintLog).where(PrintLog.model_id == model.id).order_by(PrintLog.id.desc())).first()
            if log and (datetime.utcnow() - log.created_at).total_seconds() < 300:
                log.energy_kwh = energy_kwh
                session.add(log)
    session.commit()
    fields = {"printer": printer.name, "file": label, "progress": "", "minutes": str(round(duration / 60)) if duration else ""}
    if outcome == "done":
        _say(session, printer, "print_done", f"Model Hub: {printer.name} finished", f"{label} is done" + (f" ({round(duration / 60)} min)." if duration else "."), fields)
    else:
        _say(session, printer, "print_done", f"Model Hub: {printer.name} stopped", f"{label} did not finish (cancelled or failed).", fields)
