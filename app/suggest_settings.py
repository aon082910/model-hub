"""What probably works for a model, worked out from how its prints went: the material it printed best in, the layer height of the
sliced file it printed successfully from, and materials to stay away from. It only suggests; saving it is your choice."""
from collections import defaultdict

from sqlmodel import Session, select

from app import print_outcomes
from app.models import Filament, PrinterJob, PrintFile, PrintLog


def suggest(session: Session, model_id: int) -> dict:
    spools = {f.id: f for f in session.exec(select(Filament)).all()}
    logs = session.exec(select(PrintLog).where(PrintLog.model_id == model_id)).all()
    good = [l for l in logs if not print_outcomes.is_failed(l)]
    failed = [l for l in logs if print_outcomes.is_failed(l)]
    worked, went_wrong = defaultdict(float), defaultdict(int)
    good_by, failed_by = defaultdict(int), defaultdict(int)
    for log in good:
        spool = spools.get(log.filament_id)
        if spool and spool.material:
            worked[spool.material] += log.rating or 3                      # a better rating counts for more; no rating counts as middling
            good_by[spool.material] += 1
    for log in failed:
        spool = spools.get(log.filament_id)
        if spool and spool.material:
            failed_by[spool.material] += 1
    avoid = sorted(m for m, n in failed_by.items() if n >= 2 and n / (n + good_by.get(m, 0)) >= 0.5)
    material = max(sorted(worked), key=lambda m: worked[m]) if worked else None
    if material in avoid:
        material = next((m for m in sorted(worked, key=lambda m: -worked[m]) if m not in avoid), None)
    out = {"based_on": len(good), "avoid": avoid}
    done = session.exec(select(PrinterJob).where(PrinterJob.model_id == model_id, PrinterJob.outcome == "done", PrinterJob.print_file_id.is_not(None))
                        .order_by(PrinterJob.id.desc())).first()
    kept = session.get(PrintFile, done.print_file_id) if done else None
    if kept:
        out["from_file"] = kept.filename
        if kept.layer_height:
            out["layer_height"] = kept.layer_height
        if kept.slicer:
            out["profile"] = kept.slicer
        if not material and kept.filament_type:
            material = kept.filament_type
    if material:
        out["material"] = material
    return out
