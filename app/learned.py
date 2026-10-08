"""Print times learned from your own prints.

What a slicer or the rough estimate says is only a guess; what a printer actually took is known. Two things are used:
  * the model's own history: prints of it that were measured (a printer reported the time, or you typed it in);
  * how far off estimates usually are: finished queue entries where both the estimate and the real time are known.
Nothing here changes a number by itself; it suggests one and says where it came from.
"""
import statistics
from typing import Optional

from sqlmodel import Session, select

from app import print_outcomes
from app.models import PrintFile, PrintLog, QueueItem

MIN_RATIO_SAMPLES = 3
RATIO_RANGE = (0.5, 3.0)            # a few odd prints must not make every estimate wild
# estimates that were themselves taken from history say nothing about how good the original guesses were
OWN_BASIS = ("history", "adjusted")


def model_samples(session: Session, model_id: int) -> list:
    rows = session.exec(select(PrintLog.minutes).where(PrintLog.model_id == model_id, PrintLog.measured.is_(True), print_outcomes.ok())).all()
    return [float(m) for m in rows if m and m > 0]


def slicer_minutes(session: Session, model_id: int) -> Optional[float]:
    for kept in session.exec(select(PrintFile).where(PrintFile.model_id == model_id)
                             .order_by(PrintFile.created_at.desc(), PrintFile.id.desc())).all():
        if kept.est_minutes and kept.est_minutes > 0:
            return float(kept.est_minutes)
    return None


def accuracy(session: Session) -> dict:
    """How the estimates compare with the real times: {"samples", "factor", "usable"}.
    factor 1.2 means prints take 20% longer than estimated. It is only applied once there are enough samples."""
    items = session.exec(select(QueueItem).where(QueueItem.status == "done", QueueItem.estimated_minutes > 0,
                                                 QueueItem.actual_minutes > 0)).all()
    ratios = [i.actual_minutes / i.estimated_minutes for i in items if (i.estimate_basis or "") not in OWN_BASIS]
    if not ratios:
        return {"samples": 0, "factor": None, "usable": False}
    factor = max(RATIO_RANGE[0], min(RATIO_RANGE[1], statistics.median(ratios)))
    return {"samples": len(ratios), "factor": round(factor, 2), "usable": len(ratios) >= MIN_RATIO_SAMPLES}


def suggest(session: Session, model_id: int, base: Optional[float] = None) -> dict:
    """The best guess for how long printing this model takes: {"minutes", "basis", "samples", ...}.
    basis: history (its own measured prints), adjusted (a slicer or rough figure corrected by how off estimates
    usually are), estimate (as given), or None when nothing is known."""
    samples = model_samples(session, model_id)
    if samples:
        return {"minutes": round(statistics.median(samples)), "basis": "history", "samples": len(samples)}
    given = base if base and base > 0 else slicer_minutes(session, model_id)
    if not given:
        return {"minutes": None, "basis": None, "samples": 0}
    acc = accuracy(session)
    if acc["usable"]:
        return {"minutes": round(given * acc["factor"]), "basis": "adjusted", "samples": acc["samples"],
                "base": round(given), "factor": acc["factor"]}
    return {"minutes": round(given), "basis": "estimate", "samples": 0}
