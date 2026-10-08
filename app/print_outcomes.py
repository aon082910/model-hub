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


TIPS = {
    "bed_adhesion": "Clean the plate, level again, and try a brim or a few degrees more on the first layer.",
    "warping": "Try a brim, a hotter bed, less draught (an enclosure) or a different material.",
    "spaghetti": "Something came loose: more bed adhesion, slower first layers, or supports under the overhangs.",
    "layer_shift": "Check the belts and pulleys, lower the speed and acceleration, and look for something catching the head.",
    "clog": "Check the nozzle and the extruder, dry the filament, and try a slightly hotter nozzle.",
    "ran_out": "Weigh the spool before you start, and find out why it tangled.",
    "supports": "Change the support density or distance, or print it in another orientation.",
    "quality": "Dry the filament, lower the speed and tune retraction.",
    "power": "A small UPS or a more reliable socket helps, and so does power-loss recovery in the firmware.",
}


def hints(session, model_id=None) -> dict:
    """{model id: what went wrong with it before}, for models that have failed at least once (or just model_id).
    {prints, failures, attempts, rate, top_reason, top_label, tip, materials: [{material, failures}], last_failure}."""
    from collections import defaultdict
    from sqlmodel import select
    from app.models import Filament
    stmt = select(PrintLog)
    if model_id is not None:
        stmt = stmt.where(PrintLog.model_id == model_id)
    seen = defaultdict(lambda: {"ok": 0, "failed": [], "last": None})
    for log in session.exec(stmt).all():
        slot = seen[log.model_id]
        if is_failed(log):
            slot["failed"].append(log)
            slot["last"] = max(slot["last"] or log.printed_at, log.printed_at)
        else:
            slot["ok"] += 1
    spools = {f.id: f for f in session.exec(select(Filament)).all()}
    out = {}
    for mid, slot in seen.items():
        failed = slot["failed"]
        if not failed:
            continue
        reasons = defaultdict(int)
        materials = defaultdict(int)
        for log in failed:
            if log.failure_reason:
                reasons[log.failure_reason] += 1
            spool = spools.get(log.filament_id)
            if spool and spool.material:
                materials[spool.material] += 1
        top = max(sorted(reasons), key=lambda r: reasons[r]) if reasons else None
        attempts = slot["ok"] + len(failed)
        out[mid] = {
            "prints": slot["ok"], "failures": len(failed), "attempts": attempts, "rate": round(len(failed) / attempts * 100),
            "top_reason": top, "top_label": label(top), "tip": TIPS.get(top) if top else None,
            "materials": [{"material": m, "failures": n} for m, n in sorted(materials.items(), key=lambda kv: (-kv[1], kv[0]))],
            "last_failure": slot["last"].isoformat() if slot["last"] else None,
        }
    return out


def ok():
    """A SQL condition: this log entry is a print that worked."""
    return or_(PrintLog.outcome.is_(None), PrintLog.outcome != "failed")


def failed():
    return PrintLog.outcome == "failed"


def is_failed(log) -> bool:
    return getattr(log, "outcome", None) == "failed"


def label(reason: Optional[str]) -> Optional[str]:
    return REASONS.get(reason) if reason else None
