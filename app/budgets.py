"""Budgets (cost centres): money is reserved when a print is queued, charged when it finishes, and (if asked) a dispatch is refused once it is spent.

The cost is the cost calculator's (Settings, Costs). A finished print is charged what it cost; a failed one only the filament that went into it.
Nothing is blocked unless a budget is set to stop; prints with no known grams or time cannot be priced, so they reserve nothing (and are counted as unpriced)."""
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app import costing, slots
from app.models import CostCentre, LedgerEntry, Order, QueueItem

TOLERANCE = 0.005


def period_start(centre: CostCentre, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, 1) if centre.period == "month" else None


def estimate(session: Session, item: QueueItem) -> Optional[float]:
    """What this waiting print is expected to cost, or None when its grams or time are not known."""
    if not item.estimated_grams or not item.estimated_minutes:
        return None
    parts = slots.spool_parts(session, item)
    spool = parts[0][0] if parts else slots.effective_filament(session, item)
    return costing.quote(session, float(item.estimated_grams), float(item.estimated_minutes), spool)["unit"]["cost"]


def summary(session: Session, centre: CostCentre, now: Optional[datetime] = None) -> dict:
    start = period_start(centre, now)
    entries = session.exec(select(LedgerEntry).where(LedgerEntry.cost_centre_id == centre.id)).all()
    spent = sum(e.amount for e in entries if start is None or e.created_at >= start)
    reserved, unpriced = 0.0, 0
    for item in session.exec(select(QueueItem).where(QueueItem.cost_centre_id == centre.id, QueueItem.status.in_(["queued", "printing"]))).all():
        cost = estimate(session, item)
        if cost is None:
            unpriced += 1
        else:
            reserved += cost
    return {"id": centre.id, "name": centre.name, "budget": round(centre.budget, 2), "period": centre.period, "hard_stop": bool(centre.hard_stop),
            "spent": round(spent, 2), "reserved": round(reserved, 2), "available": round(centre.budget - spent - reserved, 2), "unpriced": unpriced}


def over_message(session: Session, centre_id: Optional[int]) -> Optional[str]:
    """Why a budget that is set to stop should refuse more (spent plus reserved is over what it holds), or None."""
    centre = session.get(CostCentre, centre_id) if centre_id else None
    if not centre or not centre.hard_stop:
        return None
    info = summary(session, centre)
    if info["available"] < -TOLERANCE:
        return f"The budget \"{centre.name}\" is over by {-info['available']:.2f} (budget {info['budget']:.2f}, spent {info['spent']:.2f}, reserved for queued prints {info['reserved']:.2f})"
    return None


def _charge(session: Session, item: QueueItem, amount: float, kind: str, note: str) -> None:
    if not item.cost_centre_id or not session.get(CostCentre, item.cost_centre_id) or amount <= 0:
        return
    if session.exec(select(LedgerEntry.id).where(LedgerEntry.queue_item_id == item.id, LedgerEntry.kind == kind)).first():
        return
    session.add(LedgerEntry(cost_centre_id=item.cost_centre_id, queue_item_id=item.id, amount=round(amount, 2), kind=kind, note=note[:200]))


def charge_done(session: Session, item: QueueItem, grams: Optional[float], minutes: Optional[float]) -> None:
    grams = grams or item.estimated_grams
    minutes = minutes or item.actual_minutes or item.estimated_minutes
    if not grams or not minutes:
        return
    spool = slots.effective_filament(session, item)
    _charge(session, item, costing.quote(session, float(grams), float(minutes), spool)["unit"]["cost"], "print", f"Print of model {item.model_id}")


def charge_failed(session: Session, item: QueueItem, grams_used: float) -> None:
    """A failed print costs the filament that went into it, not the machine time or the margin."""
    if not grams_used or grams_used <= 0:
        return
    filament = costing.quote(session, float(grams_used), 0, slots.effective_filament(session, item))["unit"]["filament"]
    _charge(session, item, filament or 0, "failed", f"Filament of a failed print of model {item.model_id}")


def forget_centre(session: Session, centre_id: int) -> None:
    for item in session.exec(select(QueueItem).where(QueueItem.cost_centre_id == centre_id)).all():
        item.cost_centre_id = None
        session.add(item)
    for order in session.exec(select(Order).where(Order.cost_centre_id == centre_id)).all():
        order.cost_centre_id = None
        session.add(order)
    for row in session.exec(select(LedgerEntry).where(LedgerEntry.cost_centre_id == centre_id)).all():
        session.delete(row)                                      # SQLite reuses ids: a ledger must never pass to a later budget
