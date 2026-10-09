from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import budgets
from app.db import get_session
from app.models import CostCentre, LedgerEntry, Model3D, QueueItem

router = APIRouter(prefix="/api/budgets", tags=["budgets"])
MAX_CENTRES = 100


def _money(value, name: str, low: float = 0, high: float = 100_000_000) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise HTTPException(400, f"{name} must be an amount of money")
    return round(float(value), 2)


def _name(session: Session, value, own_id=None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "Give the budget a name")
    name = value.strip()[:60]
    for other in session.exec(select(CostCentre)).all():
        if other.id != own_id and other.name.lower() == name.lower():
            raise HTTPException(400, "A budget with that name already exists")
    return name


def _period(value) -> str:
    if value not in ("total", "month"):
        raise HTTPException(400, "period must be total or month")
    return value


@router.get("")
def list_budgets(session: Session = Depends(get_session)):
    return {"budgets": [budgets.summary(session, c) for c in session.exec(select(CostCentre).order_by(CostCentre.name)).all()]}


@router.post("")
def create_budget(payload: dict, session: Session = Depends(get_session)):
    if len(session.exec(select(CostCentre.id)).all()) >= MAX_CENTRES:
        raise HTTPException(400, f"At most {MAX_CENTRES} budgets")
    hard = payload.get("hard_stop", False)
    if not isinstance(hard, bool):
        raise HTTPException(400, "hard_stop must be true or false")
    c = CostCentre(name=_name(session, payload.get("name")), budget=_money(payload.get("budget", 0), "budget"), period=_period(payload.get("period", "total")), hard_stop=hard)
    session.add(c)
    session.commit()
    session.refresh(c)
    return budgets.summary(session, c)


@router.patch("/{centre_id}")
def update_budget(centre_id: int, payload: dict, session: Session = Depends(get_session)):
    c = session.get(CostCentre, centre_id)
    if not c:
        raise HTTPException(404, "Not found")
    if "name" in payload:
        c.name = _name(session, payload["name"], centre_id)
    if "budget" in payload:
        c.budget = _money(payload["budget"], "budget")
    if "period" in payload:
        c.period = _period(payload["period"])
    if "hard_stop" in payload:
        if not isinstance(payload["hard_stop"], bool):
            raise HTTPException(400, "hard_stop must be true or false")
        c.hard_stop = payload["hard_stop"]
    session.add(c)
    session.commit()
    return budgets.summary(session, c)


@router.delete("/{centre_id}")
def delete_budget(centre_id: int, session: Session = Depends(get_session)):
    c = session.get(CostCentre, centre_id)
    if not c:
        raise HTTPException(404, "Not found")
    budgets.forget_centre(session, centre_id)
    session.delete(c)
    session.commit()
    return {"status": "deleted"}


@router.get("/{centre_id}/ledger")
def ledger(centre_id: int, limit: int = 200, session: Session = Depends(get_session)):
    c = session.get(CostCentre, centre_id)
    if not c:
        raise HTTPException(404, "Not found")
    rows = session.exec(select(LedgerEntry).where(LedgerEntry.cost_centre_id == centre_id).order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()).limit(max(1, min(limit, 1000)))).all()
    return {"budget": budgets.summary(session, c), "entries": [{"id": e.id, "amount": e.amount, "kind": e.kind, "note": e.note, "created_at": e.created_at.isoformat()} for e in rows]}


@router.post("/{centre_id}/entries")
def add_entry(centre_id: int, payload: dict, session: Session = Depends(get_session)):
    """Write in something spent (or a credit, as a negative amount) that Model Hub could not know."""
    c = session.get(CostCentre, centre_id)
    if not c:
        raise HTTPException(404, "Not found")
    amount = _money(payload.get("amount"), "amount", -100_000_000)
    if amount == 0:
        raise HTTPException(400, "The amount cannot be zero")
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(400, "note must be text")
    session.add(LedgerEntry(cost_centre_id=centre_id, amount=amount, kind="manual", note=(note or "").strip()[:200] or None))
    session.commit()
    return budgets.summary(session, c)


@router.delete("/{centre_id}/entries/{entry_id}")
def delete_entry(centre_id: int, entry_id: int, session: Session = Depends(get_session)):
    e = session.get(LedgerEntry, entry_id)
    if not e or e.cost_centre_id != centre_id:
        raise HTTPException(404, "Not found")
    session.delete(e)
    session.commit()
    return {"status": "deleted"}
