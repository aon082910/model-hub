"""Numbers about your printing and your library, worked out from the print log, filament prices and printer events."""
from collections import defaultdict
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import MODEL_EXTENSIONS
from app import print_outcomes
from app.models import Filament, Model3D, PrinterJob, PrintLog


def _month_keys(now: datetime, months: int) -> list:
    keys, year, month = [], now.year, now.month
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def _cost_of(log: PrintLog, spools: dict) -> float:
    spool = spools.get(log.filament_id)
    if not log.grams or not spool or spool.cost is None or not spool.spool_weight_g:
        return 0.0
    return float(log.grams) * spool.cost / spool.spool_weight_g


def build(session: Session, months: int = 12, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    keys = _month_keys(now, months)
    first = keys[0]
    spools = {f.id: f for f in session.exec(select(Filament)).all()}

    per_month = {k: {"month": k, "prints": 0, "grams": 0.0, "minutes": 0.0, "cost": 0.0} for k in keys}
    totals = {"prints": 0, "grams": 0.0, "minutes": 0.0, "cost": 0.0, "models_printed": set()}
    materials = defaultdict(float)
    by_model = defaultdict(lambda: [0, None])
    failures = {"total": 0, "grams": 0.0, "cost": 0.0, "reasons": defaultdict(int)}
    for log in session.exec(select(PrintLog)).all():
        key = log.printed_at.strftime("%Y-%m")
        in_range = key in per_month
        if print_outcomes.is_failed(log):                      # a failure is counted on its own, not as a print
            if in_range:
                failures["total"] += 1
                failures["grams"] += float(log.grams or 0)
                failures["cost"] += _cost_of(log, spools)
                failures["reasons"][log.failure_reason or "unsaid"] += 1
            continue
        cost = _cost_of(log, spools)
        grams = float(log.grams or 0)
        minutes = float(log.minutes or 0)
        if in_range:
            row = per_month[key]
            row["prints"] += 1
            row["grams"] += grams
            row["minutes"] += minutes
            row["cost"] += cost
            by_model[log.model_id][0] += 1
            by_model[log.model_id][1] = max(by_model[log.model_id][1] or log.printed_at, log.printed_at)
            if grams and spools.get(log.filament_id):
                materials[spools[log.filament_id].material or "unknown"] += grams
            totals["prints"] += 1
            totals["grams"] += grams
            totals["minutes"] += minutes
            totals["cost"] += cost
            totals["models_printed"].add(log.model_id)

    names = dict(session.exec(select(Model3D.id, Model3D.filename).where(Model3D.id.in_(list(by_model) or [0]))).all())
    top = sorted(by_model.items(), key=lambda kv: (-kv[1][0], kv[0]))[:10]

    added = defaultdict(int)
    for created in session.exec(select(Model3D.created_at).where(Model3D.extension.in_(MODEL_EXTENSIONS))).all():
        key = created.strftime("%Y-%m")
        if key >= first:
            added[key] += 1

    jobs = session.exec(select(PrinterJob.outcome, func.count()).where(PrinterJob.outcome.is_not(None)).group_by(PrinterJob.outcome)).all()
    outcomes = {o: n for o, n in jobs}
    finished = outcomes.get("done", 0) + outcomes.get("stopped", 0)

    library_total = session.exec(select(func.count()).select_from(Model3D).where(Model3D.extension.in_(MODEL_EXTENSIONS))).one()
    ever_printed = session.exec(select(func.count(func.distinct(PrintLog.model_id))).where(print_outcomes.ok())).one()
    linked = session.exec(select(func.count()).select_from(Model3D).where(Model3D.extension.in_(MODEL_EXTENSIONS),
                                                                         Model3D.source_provider.is_not(None))).one()
    spool_rows = list(spools.values())
    return {
        "months": months,
        "per_month": [{**row, "grams": round(row["grams"], 1), "minutes": round(row["minutes"]), "cost": round(row["cost"], 2),
                       "models_added": added.get(row["month"], 0)} for row in per_month.values()],
        "totals": {"prints": totals["prints"], "grams": round(totals["grams"], 1), "hours": round(totals["minutes"] / 60, 1),
                   "cost": round(totals["cost"], 2), "models_printed": len(totals["models_printed"])},
        "top_models": [{"id": mid, "filename": names.get(mid, "(removed)"), "prints": n, "last_printed": last} for mid, (n, last) in top],
        "materials": [{"material": m, "grams": round(g, 1)} for m, g in sorted(materials.items(), key=lambda kv: -kv[1])],
        "printer_jobs": {"done": outcomes.get("done", 0), "stopped": outcomes.get("stopped", 0),
                         "success_rate": round(outcomes.get("done", 0) / finished * 100, 1) if finished else None},
        "failures": {
            "total": failures["total"],
            "rate": round(failures["total"] / (failures["total"] + totals["prints"]) * 100, 1) if failures["total"] else (0.0 if totals["prints"] else None),
            "grams": round(failures["grams"], 1), "cost": round(failures["cost"], 2),
            "by_reason": [{"reason": r, "label": print_outcomes.label(r) or "No reason given", "count": n}
                          for r, n in sorted(failures["reasons"].items(), key=lambda kv: (-kv[1], kv[0]))],
        },
        "library": {"models": library_total, "ever_printed": ever_printed, "never_printed": max(0, library_total - ever_printed),
                    "linked": linked},
        "filament": {"spools": len(spool_rows), "remaining_g": round(sum(f.remaining_g for f in spool_rows), 1)},
    }
