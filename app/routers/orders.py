import csv
import io
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from app import activity, costing
from app.db import get_session
from app.models import Model3D, Order, OrderItem, QueueItem

router = APIRouter(prefix="/api/orders", tags=["orders"])

STATUSES = ("quote", "accepted", "printing", "ready", "delivered", "cancelled")
MAX_ORDERS = 2000
MAX_UNITS_QUEUED = 500


def _date(value) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(400, "The date must look like 2026-10-31")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "That is not a real date")
    return value


def _text(value, name: str, limit: int, required: bool = False) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise HTTPException(400, f"{name} is needed")
        return None
    if not isinstance(value, str):
        raise HTTPException(400, f"{name} must be text")
    return value.strip()[:limit]


def _quantity(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
        raise HTTPException(400, "quantity must be a whole number from 1 to 10000")
    return value


def _price(value) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10_000_000:
        raise HTTPException(400, "unit_price must be an amount of money")
    return round(float(value), 2)


def _item_json(session: Session, item: OrderItem, names: dict, units: dict) -> dict:
    defaults = costing.defaults_for(session, item.model_id)
    q = None
    if defaults["grams"] is not None and defaults["minutes"] is not None:
        q = costing.quote(session, defaults["grams"], defaults["minutes"], defaults["filament_id"], item.quantity)
    suggested = q["unit"]["price"] if q else None
    used = item.unit_price if item.unit_price is not None else suggested
    queued = units.get(item.id, {"queued": 0, "done": 0})
    return {"id": item.id, "model_id": item.model_id, "filename": names.get(item.model_id), "quantity": item.quantity, "unit_price": item.unit_price,
            "suggested_price": suggested, "price_used": used, "line_price": round(used * item.quantity, 2) if used is not None else None,
            "line_cost": q["total"]["cost"] if q else None, "units_queued": queued["queued"], "units_done": queued["done"], "estimated": q is not None}


def _unit_counts(session: Session, order_id: int) -> dict:
    counts: dict = {}
    for q in session.exec(select(QueueItem).where(QueueItem.order_id == order_id)).all():
        c = counts.setdefault(q.order_item_id, {"queued": 0, "done": 0})
        if q.status != "failed":
            c["queued"] += 1
        if q.status == "done":
            c["done"] += 1
    return counts


def _order_json(session: Session, order: Order, with_items: bool = True) -> dict:
    items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)).all()
    names = {m.id: m.filename for m in session.exec(select(Model3D).where(Model3D.id.in_({i.model_id for i in items} or {0}))).all()}
    units = _unit_counts(session, order.id)
    rows = [_item_json(session, i, names, units) for i in items]
    wanted = sum(r["quantity"] for r in rows)
    done = sum(r["units_done"] for r in rows)
    price = sum(r["line_price"] for r in rows if r["line_price"] is not None)
    cost = sum(r["line_cost"] for r in rows if r["line_cost"] is not None)
    suggested = order.status
    if wanted and done >= wanted and order.status in ("accepted", "printing"):
        suggested = "ready"
    elif done and order.status == "accepted":
        suggested = "printing"
    out = {"id": order.id, "customer": order.customer, "contact": order.contact, "status": order.status, "due_date": order.due_date, "notes": order.notes,
           "paid": order.paid, "created_at": order.created_at.isoformat(), "units": wanted, "units_done": done,
           "price": round(price, 2), "cost": round(cost, 2), "profit": round(price - cost, 2), "suggested_status": suggested,
           "overdue": bool(order.due_date and order.due_date < date.today().isoformat() and order.status in ("quote", "accepted", "printing"))}
    if with_items:
        out["items"] = rows
    return out


@router.get("")
def list_orders(status: Optional[str] = None, session: Session = Depends(get_session)):
    stmt = select(Order).order_by(Order.id.desc())
    if status:
        if status not in STATUSES:
            raise HTTPException(400, "Unknown status")
        stmt = stmt.where(Order.status == status)
    return {"orders": [_order_json(session, o, with_items=False) for o in session.exec(stmt).all()], "statuses": list(STATUSES)}


@router.post("")
def create_order(payload: dict, request: Request, session: Session = Depends(get_session)):
    if len(session.exec(select(Order.id)).all()) >= MAX_ORDERS:
        raise HTTPException(400, f"At most {MAX_ORDERS} orders")
    order = Order(customer=_text(payload.get("customer"), "customer", 120, True), contact=_text(payload.get("contact"), "contact", 200),
                  due_date=_date(payload.get("due_date")), notes=_text(payload.get("notes"), "notes", 4000))
    items = payload.get("items") or []
    if not isinstance(items, list) or len(items) > 100:
        raise HTTPException(400, "items must be a list of at most 100 models")
    checked = []
    for row in items:
        if not isinstance(row, dict) or not isinstance(row.get("model_id"), int) or not session.get(Model3D, row["model_id"]):
            raise HTTPException(400, "Each item needs the id of a model in the library")
        checked.append((row["model_id"], _quantity(row.get("quantity", 1)), _price(row.get("unit_price"))))
    session.add(order)
    session.commit()
    session.refresh(order)
    for model_id, quantity, price in checked:
        session.add(OrderItem(order_id=order.id, model_id=model_id, quantity=quantity, unit_price=price))
    session.commit()
    activity.record(session, activity.actor_of(request), "order", f"Started an order for {order.customer}")
    return _order_json(session, order)


def _get(session: Session, order_id: int) -> Order:
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Not found")
    return order


@router.get("/export.csv")
def export_csv(session: Session = Depends(get_session)):
    """Every order as a spreadsheet (cells that start like a formula are stored as text)."""
    def safe(value):
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["order", "customer", "contact", "status", "due", "paid", "model", "quantity", "unit price", "line price", "line cost", "notes"])
    for order in session.exec(select(Order).order_by(Order.id)).all():
        detail = _order_json(session, order)
        for item in detail["items"] or [None]:
            writer.writerow([order.id, safe(order.customer), safe(order.contact), order.status, order.due_date or "", "yes" if order.paid else "no",
                             safe(item["filename"]) if item else "", item["quantity"] if item else "", item["price_used"] if item else "",
                             item["line_price"] if item else "", item["line_cost"] if item else "", safe(order.notes)])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="modelhub-orders.csv"'})


@router.get("/{order_id}")
def get_order(order_id: int, session: Session = Depends(get_session)):
    return _order_json(session, _get(session, order_id))


@router.patch("/{order_id}")
def update_order(order_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    order = _get(session, order_id)
    before = order.status
    if "customer" in payload:
        order.customer = _text(payload["customer"], "customer", 120, True)
    if "contact" in payload:
        order.contact = _text(payload["contact"], "contact", 200)
    if "notes" in payload:
        order.notes = _text(payload["notes"], "notes", 4000)
    if "due_date" in payload:
        order.due_date = _date(payload["due_date"])
    if "status" in payload:
        if payload["status"] not in STATUSES:
            raise HTTPException(400, "status must be one of: " + ", ".join(STATUSES))
        order.status = payload["status"]
    if "paid" in payload:
        if not isinstance(payload["paid"], bool):
            raise HTTPException(400, "paid must be true or false")
        order.paid = payload["paid"]
    session.add(order)
    session.commit()
    if order.status != before:
        activity.record(session, activity.actor_of(request), "order", f"Order {order.id} for {order.customer}: {before} -> {order.status}")
    return _order_json(session, order)


@router.delete("/{order_id}")
def delete_order(order_id: int, request: Request, session: Session = Depends(get_session)):
    order = _get(session, order_id)
    for q in session.exec(select(QueueItem).where(QueueItem.order_id == order_id)).all():
        q.order_id = q.order_item_id = None                       # the prints stay; they are just not for an order any more
        session.add(q)
    for item in session.exec(select(OrderItem).where(OrderItem.order_id == order_id)).all():
        session.delete(item)
    name = order.customer
    session.delete(order)
    session.commit()
    activity.record(session, activity.actor_of(request), "order", f"Removed the order for {name}")
    return {"status": "deleted"}


@router.post("/{order_id}/items")
def add_item(order_id: int, payload: dict, session: Session = Depends(get_session)):
    order = _get(session, order_id)
    if not isinstance(payload.get("model_id"), int) or not session.get(Model3D, payload["model_id"]):
        raise HTTPException(400, "Choose a model from the library")
    if len(session.exec(select(OrderItem.id).where(OrderItem.order_id == order_id)).all()) >= 100:
        raise HTTPException(400, "At most 100 models in one order")
    session.add(OrderItem(order_id=order.id, model_id=payload["model_id"], quantity=_quantity(payload.get("quantity", 1)), unit_price=_price(payload.get("unit_price"))))
    session.commit()
    return _order_json(session, order)


@router.patch("/{order_id}/items/{item_id}")
def edit_item(order_id: int, item_id: int, payload: dict, session: Session = Depends(get_session)):
    order = _get(session, order_id)
    item = session.get(OrderItem, item_id)
    if not item or item.order_id != order_id:
        raise HTTPException(404, "Not found")
    if "quantity" in payload:
        item.quantity = _quantity(payload["quantity"])
    if "unit_price" in payload:
        item.unit_price = _price(payload["unit_price"])
    session.add(item)
    session.commit()
    return _order_json(session, order)


@router.delete("/{order_id}/items/{item_id}")
def remove_item(order_id: int, item_id: int, session: Session = Depends(get_session)):
    order = _get(session, order_id)
    item = session.get(OrderItem, item_id)
    if not item or item.order_id != order_id:
        raise HTTPException(404, "Not found")
    for q in session.exec(select(QueueItem).where(QueueItem.order_item_id == item_id)).all():
        q.order_id = q.order_item_id = None
        session.add(q)
    session.delete(item)
    session.commit()
    return _order_json(session, order)


@router.post("/{order_id}/queue")
def queue_order(order_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    """Put what the order still needs into the print queue: one entry per unit that is not queued yet (a failed entry does not count).
    printer_id sends them all to one printer."""
    from app import learned
    from app.models import Printer
    order = _get(session, order_id)
    printer_id = payload.get("printer_id")
    if printer_id is not None and not session.get(Printer, printer_id):
        raise HTTPException(400, "That printer does not exist")
    units = _unit_counts(session, order_id)
    top = session.exec(select(QueueItem).order_by(QueueItem.position.desc())).first()
    position = (top.position + 1) if top else 0
    created = 0
    for item in session.exec(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)).all():
        for _ in range(max(0, item.quantity - units.get(item.id, {"queued": 0})["queued"])):
            if created >= MAX_UNITS_QUEUED:
                raise HTTPException(400, f"That would add more than {MAX_UNITS_QUEUED} entries at once")
            defaults = costing.defaults_for(session, item.model_id)
            suggestion = learned.suggest(session, item.model_id)
            session.add(QueueItem(model_id=item.model_id, position=position, status="queued", printer_id=printer_id, order_id=order.id, order_item_id=item.id,
                                  filament_id=defaults["filament_id"], estimated_grams=defaults["grams"], estimated_minutes=suggestion["minutes"],
                                  estimate_basis=suggestion["basis"] if suggestion["minutes"] else None, planned_date=order.due_date,
                                  notes=f"Order {order.id}: {order.customer}"))
            position += 1
            created += 1
    if order.status in ("quote", "accepted") and created:
        order.status = "accepted" if order.status == "quote" else order.status
        session.add(order)
    session.commit()
    if created:
        activity.record(session, activity.actor_of(request), "order", f"Queued {created} print(s) for the order of {order.customer}")
    return {"queued": created, "order": _order_json(session, order)}
