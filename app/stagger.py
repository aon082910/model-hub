"""Keep printers from all starting at once: a minimum gap between starts and a limit on how many may print together.

Both are off until set (Settings, Print planning). They only apply when Model Hub itself is asked to start a print; a print started on the printer is not stopped.
Starting anyway is always possible, because only you know whether the power supply can take it."""
import math
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models import PrinterJob, Printer
from app.settings_store import get_setting


def _whole(session: Session, key: str) -> int:
    try:
        return max(0, int(float(get_setting(session, key, "") or 0)))
    except ValueError:
        return 0


def reason_to_wait(session: Session, printer: Printer, now: Optional[datetime] = None) -> Optional[str]:
    """Why this printer should not be started right now, or None."""
    from app import printwatch
    now = now or datetime.utcnow()
    from app import sensors
    held = sensors.hold_reason(session, printer.id)
    if held:
        return f"{printer.name} is on hold because {held}"
    gap, limit = _whole(session, "stagger_minutes"), _whole(session, "max_printing")
    if gap:
        recent = session.exec(select(PrinterJob).where(PrinterJob.started.is_(True), PrinterJob.printer_id != printer.id, PrinterJob.finished_at.is_(None),
                                                       PrinterJob.sent_at > now - timedelta(minutes=gap)).order_by(PrinterJob.sent_at.desc())).first()
        if recent:
            other = session.get(Printer, recent.printer_id)
            left = max(1, math.ceil(gap - (now - recent.sent_at).total_seconds() / 60))
            return (f"{other.name if other else 'Another printer'} started a print a moment ago, and starts are kept {gap} minutes apart so the power supply "
                    f"is not hit at once: wait {left} more minute{'s' if left != 1 else ''}")
    if limit:
        busy = [pid for pid, st in printwatch.latest.items() if pid != printer.id and st.get("state") == "printing"]
        if len(busy) >= limit:
            return f"{len(busy)} printer{'s are' if len(busy) != 1 else ' is'} already printing and at most {limit} may print at once"
    return None
