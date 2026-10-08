"""How a print turned out. A print is a success unless it was logged as failed (older entries have no outcome: they succeeded).

Failed prints are kept, with a reason, so the library can say what goes wrong and what it wasted, but they never count as a
model having been printed, never feed the learned print times and never take a model off the "never printed" list.
"""
from typing import Optional

from sqlalchemy import or_

from app.models import PrintLog

REASONS = {
    "bed_adhesion": "Would not stick to the bed",
    "warping": "Warped or curled",
    "spaghetti": "Came loose mid-print (spaghetti)",
    "layer_shift": "Layers shifted",
    "clog": "Clogged nozzle or under-extrusion",
    "ran_out": "Filament ran out or tangled",
    "supports": "Supports failed or would not come off",
    "quality": "Bad quality (stringing, blobs, rough)",
    "power": "Power or network lost",
    "cancelled": "I stopped it",
    "other": "Something else",
}


def ok():
    """A SQL condition: this log entry is a print that worked."""
    return or_(PrintLog.outcome.is_(None), PrintLog.outcome != "failed")


def failed():
    return PrintLog.outcome == "failed"


def is_failed(log) -> bool:
    return getattr(log, "outcome", None) == "failed"


def label(reason: Optional[str]) -> Optional[str]:
    return REASONS.get(reason) if reason else None
