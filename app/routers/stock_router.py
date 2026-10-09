from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app import activity, costing, learned, stock
from app.db import get_session
from app.models import Model3D, QueueItem, StockItem

router = APIRouter(prefix="/api/stock", tags=["stock"])
MAX_ITEMS = 2000
MAX_MAKE = 100


def _int(value, name: str, low: int = 0, high: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise HTTPException(400, f"{name} must be a whole number from {low} to {high}")
    return value


def _text(value, name: str, limit: int):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise HTTPException(400, f"{name} must be text")
    return value.strip()[:limit]


def _get(session: Session, stock_id: int) -> StockItem:
    row = session.get(StockItem, stock_id)
    if not row:
        raise HTTPException(404, "Not found")
    return row


@router.get("")
def list_stock(session: Session = Depends(get_session)):
    rows = stock.overview(session)
    return {"items": rows, "on_hand": sum(r["on_hand"] for r in rows), "low": sum(1 for r in rows if r["low"])}


@router.post("")
def add_stock(payload: dict, request: Request, session: Session = Depends(get_session)):
    model_id = payload.get("model_id")
    if isinstance(model_id, bool) or not isinstance(model_id, int) or not session.get(Model3D, model_id):
        raise HTTPException(400, "Choose a model from the library")
    if session.exec(select(StockItem.id).where(StockItem.model_id == model_id)).first():
        raise HTTPException(400, "That model is already on the shelf list: change its numbers instead")
    if len(session.exec(select(StockItem.id)).all()) >= MAX_ITEMS:
        raise HTTPException(400, f"At most {MAX_ITEMS} parts")
    row = StockItem(model_id=model_id, on_hand=_int(payload.get("on_hand", 0), "on_hand"), minimum=_int(payload.get("minimum", 0), "minimum"),
                    sku=_text(payload.get("sku"), "sku", 60), notes=_text(payload.get("notes"), "notes", 500))
    session.add(row)
    session.commit()
    session.refresh(row)
    return stock.describe(session, row)


@router.patch("/{stock_id}")
def update_stock(stock_id: int, payload: dict, session: Session = Depends(get_session)):
    row = _get(session, stock_id)
    if "on_hand" in payload:
        row.on_hand = _int(payload["on_hand"], "on_hand")
    if "minimum" in payload:
        row.minimum = _int(payload["minimum"], "minimum")
    if "sku" in payload:
        row.sku = _text(payload["sku"], "sku", 60)
    if "notes" in payload:
        row.notes = _text(payload["notes"], "notes", 500)
    session.add(row)
    session.commit()
    return stock.describe(session, row)


@router.delete("/{stock_id}")
def delete_stock(stock_id: int, session: Session = Depends(get_session)):
    row = _get(session, stock_id)
    for q in session.exec(select(QueueItem).where(QueueItem.to_stock_id == stock_id)).all():
        q.to_stock_id = None                                  # SQLite reuses ids: a queued print must not feed a later shelf entry
        session.add(q)
    session.delete(row)
    session.commit()
    return {"status": "deleted"}


@router.post("/{stock_id}/adjust")
def adjust(stock_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    """Take some off the shelf (a negative number: sold, given away, broken) or put some on (positive)."""
    row = _get(session, stock_id)
    delta = _int(payload.get("delta"), "delta", -1_000_000, 1_000_000)
    if delta == 0:
        raise HTTPException(400, "delta cannot be zero")
    if row.on_hand + delta < 0:
        raise HTTPException(400, f"Only {row.on_hand} on the shelf")
    row.on_hand += delta
    session.add(row)
    session.commit()
    model = session.get(Model3D, row.model_id)
    activity.record(session, activity.actor_of(request), "stock", f"Stock of {model.filename if model else 'a part'}: {'+' if delta > 0 else ''}{delta} (now {row.on_hand})")
    if delta < 0:
        stock.auto_restock(session)                   # something left the shelf: refill it if that is switched on
    return stock.describe(session, row)


@router.post("/{stock_id}/make")
def make_more(stock_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    """Put prints in the queue that will add to the shelf when they finish: the shortfall (minimum minus on hand minus what is already queued), or quantity."""
    row = _get(session, stock_id)
    info = stock.describe(session, row)
    quantity = payload.get("quantity")
    quantity = _int(quantity, "quantity", 1, MAX_MAKE) if quantity is not None else info["short"]
    if quantity <= 0:
        return {"queued": 0, "item": info}
    count = stock.queue_for_shelf(session, row, quantity)
    activity.record(session, activity.actor_of(request), "stock", f"Queued {count} print(s) for the shelf")
    return {"queued": count, "item": stock.describe(session, row)}
