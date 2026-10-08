"""Notice when a printer finishes (or stops) a print.

Every poll looks at each printer's state. When one that was printing is not any more:
  * finished: if the file was one Model Hub sent for a model, the print is recorded for it (the
    matching print-queue entry is completed, which takes the filament off the spool; otherwise a
    print log entry with the print time is written), and you are notified.
  * stopped (cancelled or failed): you are notified; nothing is logged.
Nothing is ever started or controlled by this. State is kept in memory only, so a print that
ends while Model Hub was restarting is not noticed.
"""
import logging
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app import printers as printing
from app.models import Model3D, Printer, PrinterJob, QueueItem
from app.notify import notify_event

logger = logging.getLogger("modelhub.printwatch")

_last: dict = {}          # printer id -> {"state", "file"}


def reset() -> None:
    _last.clear()


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
        previous = _last.get(printer.id)
        if st["online"]:
            _last[printer.id] = {"state": st["state"], "file": st["file"] or (previous or {}).get("file")}
        if not st["online"] or not previous or previous["state"] != "printing" or st["state"] == "printing":
            continue
        outcome = "done" if finished(printer.kind, st["state"], st["progress"]) else "stopped"
        _record(session, printer, previous.get("file"), outcome, st.get("duration"))
        ended.append((printer.name, outcome))
    for gone in set(_last) - {p.id for p in printers}:
        _last.pop(gone, None)
    return ended


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
    if outcome == "done" and model:
        from app.routers.prints import log_print
        from app.routers.queue import complete_item
        minutes = duration / 60 if duration else None
        waiting = session.exec(select(QueueItem).where(QueueItem.model_id == model.id, QueueItem.status.in_(["queued", "printing"]))
                               .order_by(QueueItem.position)).first()
        if waiting:
            complete_item(session, waiting, minutes)
        else:
            log_print(session, model.id, minutes=minutes, source="printer", deduct=False)
        label = model.filename
    session.commit()
    if outcome == "done":
        notify_event(session, "print_done", f"Model Hub: {printer.name} finished", f"{label} is done" +
                     (f" ({round(duration / 60)} min)." if duration else "."))
    else:
        notify_event(session, "print_done", f"Model Hub: {printer.name} stopped", f"{label} did not finish (cancelled or failed).")
