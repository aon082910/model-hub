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

from sqlmodel import Session, select

from app import printers as printing
from app.models import Model3D, PrintLog, Printer, PrinterJob, PrintFile, QueueItem
from app.notify import notify_event

logger = logging.getLogger("modelhub.printwatch")

_last: dict = {}          # printer id -> {"state", "file"}
latest: dict = {}         # printer id -> the full status from the most recent poll


def reset() -> None:
    _last.clear()
    latest.clear()


def finished(kind: str, state: str, progress) -> bool:
    """Did the print that just ended complete? (Moonraker says so; OctoPrint is judged by its progress.)"""
    if kind == "moonraker":
        return state == "complete"
    return isinstance(progress, (int, float)) and progress >= 99.5


def poll(session: Session) -> list:
    """Look at every printer once. Returns [(printer name, outcome)] for prints that ended."""
    ended = []
    printers = session.exec(select(Printer)).all()
    for printer in printers:
        st = printing.status(printer.kind, printer.url, printer.api_key)
        latest[printer.id] = {**st, "name": printer.name}
        previous = _last.get(printer.id)
        if st["online"]:
            _last[printer.id] = {"state": st["state"], "file": st["file"] or (previous or {}).get("file")}
        if not st["online"] or not previous or previous["state"] != "printing" or st["state"] == "printing":
            continue
        outcome = "done" if finished(printer.kind, st["state"], st["progress"]) else "stopped"
        _record(session, printer, previous.get("file"), outcome, st.get("duration"))
        ended.append((printer.name, outcome))
    for gone in (set(_last) | set(latest)) - {p.id for p in printers}:
        _last.pop(gone, None)
        latest.pop(gone, None)
    return ended


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


def _record(session: Session, printer: Printer, filename, outcome: str, duration) -> None:
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
                complete_item(session, waiting, minutes)
            else:
                kept = session.get(PrintFile, job.print_file_id) if job and job.print_file_id else None
                log_print(session, model.id, minutes=minutes, grams=kept.est_grams if kept else None, source="printer", deduct=False,
                          measured=True)
            session.flush()
            _photograph(session, printer, model.id)
        elif waiting:
            fail_item(session, waiting, minutes=minutes)        # stopped or failed: the log keeps it, with the reason still to be said
        else:
            log_print(session, model.id, minutes=round(minutes, 1) if minutes else None, source="printer", deduct=False, outcome="failed")
        if outcome == "stopped":
            session.flush()
            _photograph(session, printer, model.id)             # a picture of what went wrong is worth keeping too
        label = model.filename
    session.commit()
    if outcome == "done":
        notify_event(session, "print_done", f"Model Hub: {printer.name} finished", f"{label} is done" +
                     (f" ({round(duration / 60)} min)." if duration else "."))
    else:
        notify_event(session, "print_done", f"Model Hub: {printer.name} stopped", f"{label} did not finish (cancelled or failed).")
