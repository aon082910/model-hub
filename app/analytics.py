"""A report on your printing for a stretch of time, with filters: jobs, success rate, print time, filament, cost, printer use, by printer, material, model and customer.

Cost is what the cost calculator counts for the print itself (filament, electricity, machine time; no failure allowance or margin), and a failed print costs only the
filament that went into it. A print with no spool or price known counts as zero for the money, so the figures are a floor, not a guess."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app import costing, print_outcomes
from app.models import Filament, Model3D, Order, PrintLog, Printer, QueueItem


def _money(session: Session) -> dict:
    return {"kwh": costing.number(session, "cost_kwh_price"), "watts": costing.number(session, "cost_printer_watts", costing.DEFAULT_WATTS) or 0.0,
            "machine": costing.number(session, "cost_machine_per_hour", 0.0) or 0.0}


def _cost(log: PrintLog, failed: bool, rate_for, money: dict) -> float:
    rate, _ = rate_for(log.filament_id)
    filament = float(log.grams or 0) * rate if rate is not None else 0.0
    if failed:
        return filament
    hours = float(log.minutes or 0) / 60
    electricity = hours * money["watts"] / 1000 * money["kwh"] if money["kwh"] is not None else 0.0
    return filament + electricity + hours * money["machine"]


def build(session: Session, start: Optional[date] = None, end: Optional[date] = None, printer_id: Optional[int] = None, model_id: Optional[int] = None,
          material: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    end = end or now.date()
    start = start or end - timedelta(days=29)
    stmt = select(PrintLog).where(PrintLog.printed_at >= datetime.combine(start, datetime.min.time()), PrintLog.printed_at < datetime.combine(end + timedelta(days=1), datetime.min.time()))
    if printer_id is not None:
        stmt = stmt.where(PrintLog.printer_id == printer_id)
    if model_id is not None:
        stmt = stmt.where(PrintLog.model_id == model_id)
    spools = {f.id: f for f in session.exec(select(Filament)).all()}
    rate_cache: dict = {}

    def rate_for(filament_id):
        if filament_id not in rate_cache:
            rate_cache[filament_id] = costing.per_gram(session, filament_id)
        return rate_cache[filament_id]
    money = _money(session)
    printers = {p.id: p.name for p in session.exec(select(Printer)).all()}
    queue_order = {q.id: q.order_id for q in session.exec(select(QueueItem).where(QueueItem.order_id.is_not(None))).all()}
    customers = {o.id: o.customer for o in session.exec(select(Order)).all()}
    models = {m.id: m.filename for m in session.exec(select(Model3D)).all()}

    def blank():
        return {"jobs": 0, "ok": 0, "failed": 0, "minutes": 0.0, "grams": 0.0, "cost": 0.0}

    total, per_day, by_printer, by_material, by_model, by_customer = blank(), defaultdict(blank), defaultdict(blank), defaultdict(blank), defaultdict(blank), defaultdict(blank)
    reasons, run_minutes = defaultdict(int), 0.0
    for log in session.exec(stmt).all():
        spool = spools.get(log.filament_id)
        mat = (spool.material if spool and spool.material else "unknown")
        if material and mat.lower() != material.lower():
            continue
        failed = print_outcomes.is_failed(log)
        cost = _cost(log, failed, rate_for, money)
        if log.printer_id:
            run_minutes += float(log.minutes or 0)
        targets = [total, per_day[log.printed_at.date().isoformat()], by_printer[log.printer_id], by_material[mat], by_model[log.model_id]]
        order_id = queue_order.get(log.queue_item_id)
        if order_id in customers:
            targets.append(by_customer[customers[order_id]])
        for t in targets:
            t["jobs"] += 1
            t["failed" if failed else "ok"] += 1
            t["minutes"] += float(log.minutes or 0)
            t["grams"] += float(log.grams or 0)
            t["cost"] += cost
        if failed:
            reasons[log.failure_reason or "unsaid"] += 1

    days = (end - start).days + 1
    hours_per_day = costing.number(session, "calendar_hours_per_day", 12.0) or 12.0

    def fin(t, with_rate=True):
        out = {"jobs": t["jobs"], "ok": t["ok"], "failed": t["failed"], "hours": round(t["minutes"] / 60, 1), "grams": round(t["grams"], 1), "cost": round(t["cost"], 2)}
        if with_rate:
            out["success_rate"] = round(t["ok"] / t["jobs"] * 100, 1) if t["jobs"] else None
        return out

    return {
        "start": start.isoformat(), "end": end.isoformat(), "days": days,
        "cards": {**fin(total), "prints_per_day": round(total["jobs"] / days, 2), "average_minutes": round(total["minutes"] / total["jobs"], 1) if total["jobs"] else None,
                  "printer_run_hours": round(run_minutes / 60, 1)},
        "per_day": [{"date": (start + timedelta(days=i)).isoformat(), **fin(per_day[(start + timedelta(days=i)).isoformat()], False)} for i in range(days)],
        "by_printer": sorted(({"printer_id": pid, "printer": printers.get(pid) or "Not tied to a printer", **fin(t),
                               "utilisation": round(t["minutes"] / 60 / (days * hours_per_day) * 100, 1) if pid else None} for pid, t in by_printer.items()), key=lambda r: (-r["hours"], r["printer"])),
        "by_material": sorted(({"material": m, **fin(t)} for m, t in by_material.items()), key=lambda r: -r["grams"]),
        "by_model": sorted(({"model_id": mid, "filename": models.get(mid, "(removed)"), **fin(t)} for mid, t in by_model.items()), key=lambda r: (-r["jobs"], r["model_id"]))[:10],
        "by_customer": sorted(({"customer": c, **fin(t)} for c, t in by_customer.items()), key=lambda r: -r["cost"]),
        "failure_reasons": [{"reason": r, "label": print_outcomes.label(r) or "No reason given", "count": n} for r, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))],
        "hours_per_day": hours_per_day,
    }
