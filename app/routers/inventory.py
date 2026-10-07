import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select
from app.db import get_session
from app.models import InventoryItem

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

CATEGORIES = ("electronics", "parts", "supplies")


def _text(value, field: str, required: bool = False) -> Optional[str]:
    if value is None:
        if required:
            raise HTTPException(400, f"{field} is required")
        return None
    if not isinstance(value, str):
        raise HTTPException(400, f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise HTTPException(400, f"{field} is required")
    return value or None


def _int(value, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a whole number")
    if number < 0:
        raise HTTPException(400, f"{field} cannot be negative")
    return number


def _cost(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "unit_cost must be a number")
    if cost < 0:
        raise HTTPException(400, "unit_cost cannot be negative")
    return cost


def _category(value) -> str:
    if value not in CATEGORIES:
        raise HTTPException(400, f"category must be one of: {', '.join(CATEGORIES)}")
    return value


def _json(item: InventoryItem) -> dict:
    return {**item.model_dump(), "low_stock": bool(item.min_quantity) and item.quantity <= item.min_quantity}


def _get(session: Session, item_id: int) -> InventoryItem:
    item = session.get(InventoryItem, item_id)
    if not item:
        raise HTTPException(404, "Not found")
    return item


@router.get("")
def list_inventory(
    q: Optional[str] = None,
    category: Optional[str] = None,
    low_stock: bool = False,
    session: Session = Depends(get_session),
):
    stmt = select(InventoryItem).order_by(InventoryItem.category, InventoryItem.name)
    if category:
        stmt = stmt.where(InventoryItem.category == _category(category))
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(InventoryItem.name.ilike(pattern) | InventoryItem.location.ilike(pattern)
                          | InventoryItem.notes.ilike(pattern))
    items = [_json(i) for i in session.exec(stmt).all()]
    if low_stock:
        items = [i for i in items if i["low_stock"]]
    return items


@router.get("/export")
def export_inventory(session: Session = Depends(get_session)):
    from app.routers.projects import _spreadsheet_safe

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Name", "Type", "Quantity", "Low stock at", "Location", "Unit cost", "Link", "Notes"])
    for i in session.exec(select(InventoryItem).order_by(InventoryItem.category, InventoryItem.name)).all():
        writer.writerow([
            _spreadsheet_safe(i.name), i.category, i.quantity, i.min_quantity or "",
            _spreadsheet_safe(i.location), "" if i.unit_cost is None else f"{i.unit_cost:.2f}",
            _spreadsheet_safe(i.purchase_url), _spreadsheet_safe(i.notes),
        ])
    filename = f"inventory-{datetime.utcnow().strftime('%Y-%m-%d')}.csv"
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("")
def create_item(payload: dict, session: Session = Depends(get_session)):
    item = InventoryItem(
        name=_text(payload.get("name"), "name", required=True),
        category=_category(payload.get("category", "electronics")),
        quantity=_int(payload.get("quantity", 0), "quantity"),
        min_quantity=_int(payload.get("min_quantity", 0), "min_quantity"),
        location=_text(payload.get("location"), "location"),
        unit_cost=_cost(payload.get("unit_cost")),
        purchase_url=_text(payload.get("purchase_url"), "purchase_url"),
        notes=_text(payload.get("notes"), "notes"),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return _json(item)


@router.patch("/{item_id}")
def update_item(item_id: int, payload: dict, session: Session = Depends(get_session)):
    item = _get(session, item_id)
    if "name" in payload:
        item.name = _text(payload["name"], "name", required=True)
    if "category" in payload:
        item.category = _category(payload["category"])
    if "quantity" in payload:
        item.quantity = _int(payload["quantity"], "quantity")
    if "min_quantity" in payload:
        item.min_quantity = _int(payload["min_quantity"], "min_quantity")
    if "location" in payload:
        item.location = _text(payload["location"], "location")
    if "unit_cost" in payload:
        item.unit_cost = _cost(payload["unit_cost"])
    if "purchase_url" in payload:
        item.purchase_url = _text(payload["purchase_url"], "purchase_url")
    if "notes" in payload:
        item.notes = _text(payload["notes"], "notes")
    item.updated_at = datetime.utcnow()
    session.add(item)
    session.commit()
    session.refresh(item)
    return _json(item)


@router.delete("/{item_id}")
def delete_item(item_id: int, session: Session = Depends(get_session)):
    session.delete(_get(session, item_id))
    session.commit()
    return {"status": "deleted"}
