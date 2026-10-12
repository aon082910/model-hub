"""Vendors, purchase orders and the buy list (see app.purchasing)."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app import activity, purchasing
from app.db import get_session
from app.models import PurchaseLine, PurchaseOrder, Vendor

router = APIRouter(prefix="/api/purchasing", tags=["purchasing"])
MAX_VENDORS = 300
MAX_ORDERS = 3000


def _vendor(session: Session, vendor_id: int) -> Vendor:
    row = session.get(Vendor, vendor_id)
    if not row:
        raise HTTPException(404, "Not found")
    return row


def _order(session: Session, order_id: int) -> PurchaseOrder:
    row = session.get(PurchaseOrder, order_id)
    if not row:
        raise HTTPException(404, "Not found")
    return row


def _bad(e: purchasing.PurchaseError) -> HTTPException:
    return HTTPException(400, str(e))


def _editable(po: PurchaseOrder) -> None:
    if po.status in ("received", "cancelled"):
        raise HTTPException(409, f"A {po.status} order cannot be changed")


# ---------------------------------------------------------------- vendors
@router.get("/vendors")
def list_vendors(session: Session = Depends(get_session)):
    return {"vendors": [v.model_dump() for v in session.exec(select(Vendor).order_by(Vendor.name)).all()]}


@router.post("/vendors")
def add_vendor(payload: dict, request: Request, session: Session = Depends(get_session)):
    if len(session.exec(select(Vendor.id)).all()) >= MAX_VENDORS:
        raise HTTPException(400, f"At most {MAX_VENDORS} vendors")
    try:
        data = purchasing.clean_vendor(payload)
    except purchasing.PurchaseError as e:
        raise _bad(e)
    if session.exec(select(Vendor).where(Vendor.name == data["name"])).first():
        raise HTTPException(409, "A vendor has that name already")
    row = Vendor(**data)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@router.patch("/vendors/{vendor_id}")
def edit_vendor(vendor_id: int, payload: dict, session: Session = Depends(get_session)):
    row = _vendor(session, vendor_id)
    try:
        data = purchasing.clean_vendor({**row.model_dump(), **payload})
    except purchasing.PurchaseError as e:
        raise _bad(e)
    clash = session.exec(select(Vendor).where(Vendor.name == data["name"], Vendor.id != vendor_id)).first()
    if clash:
        raise HTTPException(409, "A vendor has that name already")
    for key, value in data.items():
        setattr(row, key, value)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@router.delete("/vendors/{vendor_id}")
def delete_vendor(vendor_id: int, session: Session = Depends(get_session)):
    row = _vendor(session, vendor_id)
    for po in session.exec(select(PurchaseOrder).where(PurchaseOrder.vendor_id == vendor_id)).all():
        po.vendor_id = None                                    # ids are reused: an order must not follow a later vendor
        session.add(po)
    session.delete(row)
    session.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------- purchase orders
@router.get("/orders")
def list_orders(status: Optional[str] = None, session: Session = Depends(get_session)):
    stmt = select(PurchaseOrder).order_by(PurchaseOrder.id.desc())
    if status:
        if status not in purchasing.STATUSES:
            raise HTTPException(400, "status must be one of: " + ", ".join(purchasing.STATUSES))
        stmt = stmt.where(PurchaseOrder.status == status)
    return {"orders": [purchasing.as_json(session, p) for p in session.exec(stmt).all()[:300]]}


def _fields(session: Session, payload: dict) -> dict:
    out = {}
    if "vendor_id" in payload:
        vid = payload["vendor_id"]
        if vid is not None and (isinstance(vid, bool) or not isinstance(vid, int) or not session.get(Vendor, vid)):
            raise HTTPException(400, "That vendor does not exist")
        out["vendor_id"] = vid
    try:
        if "reference" in payload:
            out["reference"] = purchasing._text(payload["reference"], "The reference", 80)
        if "notes" in payload:
            out["notes"] = purchasing._text(payload["notes"], "The notes", 1000)
        if "expected_on" in payload:
            out["expected_on"] = purchasing._day(payload["expected_on"])
    except purchasing.PurchaseError as e:
        raise _bad(e)
    return out


@router.post("/orders")
def make_order(payload: dict, request: Request, session: Session = Depends(get_session)):
    if len(session.exec(select(PurchaseOrder.id)).all()) >= MAX_ORDERS:
        raise HTTPException(400, f"At most {MAX_ORDERS} purchase orders")
    lines = payload.get("lines") or []
    if not isinstance(lines, list) or len(lines) > purchasing.MAX_LINES:
        raise HTTPException(400, f"lines must be a list of at most {purchasing.MAX_LINES} lines")
    po = PurchaseOrder(**_fields(session, payload))
    try:
        cleaned = [purchasing.clean_line(session, l if isinstance(l, dict) else {}) for l in lines]
    except purchasing.PurchaseError as e:
        raise _bad(e)
    session.add(po)
    session.commit()
    session.refresh(po)
    for line in cleaned:
        session.add(PurchaseLine(po_id=po.id, **line))
    session.commit()
    activity.record(session, activity.actor_of(request), "stock", f"Started purchase order {po.id}")
    return purchasing.as_json(session, po)


@router.get("/orders/{order_id}")
def get_order(order_id: int, session: Session = Depends(get_session)):
    return purchasing.as_json(session, _order(session, order_id))


@router.patch("/orders/{order_id}")
def edit_order(order_id: int, payload: dict, session: Session = Depends(get_session)):
    po = _order(session, order_id)
    _editable(po)
    for key, value in _fields(session, payload).items():
        setattr(po, key, value)
    session.add(po)
    session.commit()
    return purchasing.as_json(session, po)


@router.delete("/orders/{order_id}")
def delete_order(order_id: int, session: Session = Depends(get_session)):
    po = _order(session, order_id)
    if po.status == "received":
        raise HTTPException(409, "A received order stays as a record (it already added stock)")
    for line in session.exec(select(PurchaseLine).where(PurchaseLine.po_id == order_id)).all():
        session.delete(line)
    session.delete(po)
    session.commit()
    return {"status": "deleted"}


@router.post("/orders/{order_id}/lines")
def add_line(order_id: int, payload: dict, session: Session = Depends(get_session)):
    po = _order(session, order_id)
    _editable(po)
    if len(session.exec(select(PurchaseLine.id).where(PurchaseLine.po_id == order_id)).all()) >= purchasing.MAX_LINES:
        raise HTTPException(400, f"At most {purchasing.MAX_LINES} lines")
    try:
        session.add(PurchaseLine(po_id=order_id, **purchasing.clean_line(session, payload)))
    except purchasing.PurchaseError as e:
        raise _bad(e)
    session.commit()
    return purchasing.as_json(session, po)


@router.delete("/orders/{order_id}/lines/{line_id}")
def remove_line(order_id: int, line_id: int, session: Session = Depends(get_session)):
    po = _order(session, order_id)
    _editable(po)
    line = session.get(PurchaseLine, line_id)
    if not line or line.po_id != order_id:
        raise HTTPException(404, "Not found")
    session.delete(line)
    session.commit()
    return purchasing.as_json(session, po)


@router.post("/orders/{order_id}/status")
def set_status(order_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    """draft to ordered (when you place it) or to cancelled. Receiving has its own button."""
    po = _order(session, order_id)
    new = payload.get("status")
    if new not in ("ordered", "cancelled", "draft"):
        raise HTTPException(400, "status must be ordered, cancelled or draft (use receive for received)")
    _editable(po)
    if new == "ordered" and not session.exec(select(PurchaseLine.id).where(PurchaseLine.po_id == order_id)).first():
        raise HTTPException(400, "Add at least one line first")
    po.status = new
    if new == "ordered":
        po.ordered_on = po.ordered_on or date.today().isoformat()
    session.add(po)
    session.commit()
    activity.record(session, activity.actor_of(request), "stock", f"Purchase order {po.id} is now {new}")
    session.refresh(po)
    return purchasing.as_json(session, po)


@router.post("/orders/{order_id}/receive")
def receive_order(order_id: int, request: Request, session: Session = Depends(get_session)):
    """The goods arrived: one full spool record per spool bought, and the parts added to their supplies. Once only."""
    po = _order(session, order_id)
    try:
        done = purchasing.receive(session, po)
    except purchasing.PurchaseError as e:
        raise HTTPException(409, str(e))
    activity.record(session, activity.actor_of(request), "stock", f"Received purchase order {po.id}: {done['spools']} spool(s), {done['parts']} part(s)")
    session.refresh(po)
    return {**done, "order": purchasing.as_json(session, po)}


# ---------------------------------------------------------------- the buy list
@router.get("/buy-list")
def get_buy_list(session: Session = Depends(get_session)):
    return {"items": purchasing.buy_list(session)}


@router.post("/buy-list/order")
def order_from_buy_list(payload: dict, request: Request, session: Session = Depends(get_session)):
    """A draft purchase order from the entries you picked (items: the entries of the buy list, with the quantities you want; vendor_id optional)."""
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Pick at least one entry")
    lines = []
    for entry in items:
        if not isinstance(entry, dict):
            raise HTTPException(400, "Each entry must be an object")
        lines.append({**entry, "quantity": entry.get("quantity", entry.get("suggested", 1))})
    return make_order({"vendor_id": payload.get("vendor_id"), "lines": lines, "notes": "From the buy list"}, request, session)
