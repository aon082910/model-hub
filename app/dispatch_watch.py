"""Notice a print that was sent and started but never began.

When Model Hub sends a file with *start*, the printer should be printing within a minute or so. Sometimes the command is dropped (a printer that lost its connection, a busy Bambu printer, a
power-cycled printer) and the queue entry then sits in *printing* for ever. This looks at each job sent in the last ten minutes: if the printer never reported printing (or paused, or a finished
state) a minute after it was told to start, and now says it is idle, the entry goes back to *queued*, the job is marked *not started*, and you are told. It never starts anything again by itself.
Switch off with the setting `dispatch_watchdog` = "false"."""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app import activity
from app.models import Model3D, PrinterJob, Printer, QueueItem
from app.notify import notify_event
from app.settings_store import get_setting

logger = logging.getLogger("modelhub.dispatch")
WAIT = timedelta(seconds=60)
WINDOW = timedelta(minutes=10)
BUSY_STATES = {"printing", "paused", "complete", "cancelled", "error", "prepare", "pause", "running", "finish", "failed"}
_seen: set = set()                        # job ids the printer was seen doing something with


def reset() -> None:
    _seen.clear()


def scheduled(session: Session, now: Optional[datetime] = None) -> list:
    """Look at recent started jobs. Returns the names of prints that never began (and were put back in the queue)."""
    if get_setting(session, "dispatch_watchdog", "") == "false":
        return []
    from app import printwatch
    now = now or datetime.utcnow()
    reverted = []
    jobs = session.exec(select(PrinterJob).where(PrinterJob.started.is_(True), PrinterJob.finished_at.is_(None), PrinterJob.sent_at > now - WINDOW)).all()
    for job in jobs:
        status = printwatch.latest.get(job.printer_id) or {}
        state = str(status.get("state") or "").lower()
        if state in BUSY_STATES:
            _seen.add(job.id)
            continue
        if job.id in _seen or now - job.sent_at < WAIT or not status.get("online"):
            continue                                       # it did begin once, or it is too soon, or the printer cannot be asked
        printer = session.get(Printer, job.printer_id)
        model = session.get(Model3D, job.model_id) if job.model_id else None
        label = model.filename if model else job.filename
        item = None
        if job.model_id:
            item = session.exec(select(QueueItem).where(QueueItem.model_id == job.model_id, QueueItem.printer_id == job.printer_id, QueueItem.status == "printing")
                                .order_by(QueueItem.id)).first()
        job.finished_at, job.outcome = now, "not_started"
        session.add(job)
        if item:
            item.status = "queued"
            item.start_note = "It was sent to the printer but the printer never started it; it is back in the queue"
            session.add(item)
        session.commit()
        activity.record(session, "Model Hub", "queue", f"{label} was sent to {printer.name if printer else 'a printer'} but never started" + ("; put back in the queue" if item else ""))
        notify_event(session, "dispatch_failed", f"Model Hub: {label} did not start", f"{printer.name if printer else 'The printer'} was told to start {label} but is idle. " +
                     ("The entry is back in the queue." if item else "Check the printer."), fields={"printer": printer.name if printer else "", "file": label, "progress": "", "minutes": ""})
        reverted.append(label)
    return reverted
