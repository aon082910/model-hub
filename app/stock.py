"""Finished parts on the shelf, with a minimum: orders can take from it, a finished print can add to it, and you are told when it runs low."""
import json
from typing import Optional

from sqlmodel import Session, select

from app import costing, learned
from app.models import Model3D, QueueItem, StockItem
from app.notify import notify_event
from app.settings_store import get_setting, set_setting


def queued_for(session: Session, stock_id: int) -> int:
    return len(session.exec(select(QueueItem.id).where(QueueItem.to_stock_id == stock_id, QueueItem.status.in_(["queued", "printing"]))).all())


def describe(session: Session, row: StockItem, names: Optional[dict] = None) -> dict:
    name = (names or {}).get(row.model_id) if names is not None else (session.get(Model3D, row.model_id).filename if session.get(Model3D, row.model_id) else None)
    queued = queued_for(session, row.id)
    return {"id": row.id, "model_id": row.model_id, "filename": name, "sku": row.sku, "on_hand": row.on_hand, "minimum": row.minimum, "notes": row.notes,
            "queued": queued, "short": max(0, row.minimum - row.on_hand - queued), "low": bool(row.minimum and row.on_hand <= row.minimum)}


def overview(session: Session) -> list:
    names = {m.id: m.filename for m in session.exec(select(Model3D)).all()}
    return [describe(session, r, names) for r in session.exec(select(StockItem).order_by(StockItem.id)).all() if r.model_id in names]


MAX_MAKE = 100
AUTO_RESTOCK_CAP = 50                     # most prints one automatic round puts in the queue, all parts together


def queue_for_shelf(session: Session, row: StockItem, quantity: int) -> int:
    """Put this many prints of the part in the queue; each adds one to the shelf when it finishes. Returns how many were queued."""
    quantity = min(quantity, MAX_MAKE)
    if quantity <= 0:
        return 0
    defaults = costing.defaults_for(session, row.model_id)
    suggestion = learned.suggest(session, row.model_id)
    top = session.exec(select(QueueItem).order_by(QueueItem.position.desc())).first()
    position = (top.position + 1) if top else 0
    for _ in range(quantity):
        session.add(QueueItem(model_id=row.model_id, position=position, status="queued", filament_id=defaults["filament_id"], estimated_grams=defaults["grams"],
                              estimated_minutes=suggestion["minutes"], estimate_basis=suggestion["basis"] if suggestion["minutes"] else None, to_stock_id=row.id,
                              notes="For the shelf"))
        position += 1
    session.commit()
    return quantity


def auto_restock(session: Session) -> int:
    """With "stock_auto_restock" on: queue prints for every part whose shelf stock, counting what is already queued, is below its minimum.
    The shortfall already counts what is queued, so running this again changes nothing until something sells."""
    if get_setting(session, "stock_auto_restock", "") != "true":
        return 0
    from app import activity
    budget, made = AUTO_RESTOCK_CAP, 0
    for info in overview(session):
        if budget <= 0:
            break
        if info["short"] > 0 and info["minimum"] > 0:
            row = session.get(StockItem, info["id"])
            count = queue_for_shelf(session, row, min(info["short"], budget))
            budget -= count
            made += count
    if made:
        activity.record(session, "Model Hub", "stock", f"Queued {made} print(s) to refill the shelf")
        notify_event(session, "stock_low", "Model Hub: refilling the shelf", f"{made} print(s) were put in the queue to bring parts back to their minimum")
    return made


def on_item_done(session: Session, item: QueueItem) -> None:
    """A print made for the shelf finished: one more is on hand (once per print)."""
    if not item.to_stock_id or item.stocked:
        return
    row = session.get(StockItem, item.to_stock_id)
    if row:
        row.on_hand += 1
        session.add(row)
    item.stocked = True
    session.add(item)


def check_and_notify(session: Session) -> list:
    """Tell once about each part that fell to or below its minimum; again only after it was restocked above it and fell once more."""
    low = {r["id"]: f"{r['filename']} ({r['on_hand']} left, minimum {r['minimum']})" for r in overview(session) if r["low"]}
    try:
        before = set(json.loads(get_setting(session, "stock_notified", "[]")))
    except ValueError:
        before = set()
    fresh = [text for key, text in low.items() if key not in before]
    set_setting(session, "stock_notified", json.dumps(sorted(low)))
    if fresh:
        notify_event(session, "stock_low", "Model Hub: parts to make", "; ".join(fresh[:6]) + (f" and {len(fresh) - 6} more" if len(fresh) > 6 else ""))
    return fresh
