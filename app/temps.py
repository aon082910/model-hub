"""Temperature history: a reading of each printer's nozzle, bed and chamber about once a minute while it is printing or hot, kept for a day, for the chart on its card."""
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, delete, select

from app.models import TempSample

SAMPLE_EVERY = 55                 # seconds between readings of one printer
KEEP_HOURS = 24
MAX_POINTS = 360
_last_sample: dict = {}           # printer id -> when it was last read
_last_prune = 0.0


def reset() -> None:
    global _last_prune
    _last_sample.clear()
    _last_prune = 0.0


def record(session: Session, printer_id: int, st: dict, now: Optional[float] = None) -> bool:
    """Keep this poll's temperatures when the printer is printing or hot (at most one reading a minute). True if one was kept."""
    global _last_prune
    if not st.get("online"):
        return False
    nozzle, bed, chamber = st.get("nozzle"), st.get("bed"), st.get("chamber")
    hot = (nozzle or 0) > 50 or (bed or 0) > 35 or (chamber or 0) > 35
    if st.get("state") != "printing" and not hot:
        return False
    now = time.time() if now is None else now
    if now - _last_sample.get(printer_id, 0) < SAMPLE_EVERY:
        return False
    _last_sample[printer_id] = now
    session.add(TempSample(printer_id=printer_id, nozzle=nozzle, bed=bed, chamber=chamber))
    if now - _last_prune > 3600:
        _last_prune = now
        session.exec(delete(TempSample).where(TempSample.at < datetime.utcnow() - timedelta(hours=KEEP_HOURS)))
    session.commit()
    return True


def history(session: Session, printer_id: int, hours: float) -> dict:
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = session.exec(select(TempSample).where(TempSample.printer_id == printer_id, TempSample.at >= since).order_by(TempSample.at)).all()
    if len(rows) > MAX_POINTS:
        step = len(rows) / MAX_POINTS
        rows = [rows[int(i * step)] for i in range(MAX_POINTS)]
    return {"hours": hours, "samples": [{"t": r.at.isoformat() + "Z", "nozzle": r.nozzle, "bed": r.bed, "chamber": r.chamber} for r in rows]}
