"""Finished parts on the shelf, with a minimum: orders can take from it, a finished print can add to it, and you are told when it runs low."""
import json
from typing import Optional

from sqlmodel import Session, select

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
