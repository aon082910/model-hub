"""Buying filament and parts: vendors, purchase orders, receiving (which puts the spools and parts in stock) and a buy list.

* A **purchase order** is a list of lines for one vendor: *spool* lines (material, brand, colour, how many, price, grams per spool) and *part* lines (a supply you already track, how many, price).
  It goes draft, then ordered, then **received**: receiving makes one spool record per spool bought (full, with its price) and adds the parts to the supply's quantity. It happens once.
* The **buy list** looks at what is low: spools of a material and colour that are nearly used up (the "low filament" setting), filament that the print queue needs more of than is on hand, and
  supplies at or below their minimum. It only suggests; a draft purchase order can be made from the entries you pick.
Nothing is ordered from a shop by this: it keeps your own list and your stock right."""
import math
import re
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from app.models import Filament, InventoryItem, PurchaseLine, PurchaseOrder, QueueItem, Vendor
from app.settings_store import get_setting

MAX_LINES = 100
MAX_SPOOLS_PER_LINE = 200
STATUSES = ("draft", "ordered", "received", "cancelled")


class PurchaseError(ValueError):
    """Something to tell the person; the message is safe to show."""


def _text(value, name: str, limit: int, required: bool = False) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PurchaseError(f"{name} is needed")
        return None
    if not isinstance(value, str):
        raise PurchaseError(f"{name} must be text")
    return value.strip()[:limit]


def _day(value) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise PurchaseError("The date must look like 2026-10-31")
    return value


def _num(value, name: str, low: float, high: float, required: bool = False) -> Optional[float]:
    if value in (None, ""):
        if required:
            raise PurchaseError(f"{name} is needed")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise PurchaseError(f"{name} must be a number from {low:g} to {high:g}")
    return float(value)


def clean_vendor(payload: dict) -> dict:
    url = _text(payload.get("url"), "The web address", 300)
    if url and not re.match(r"^https?://[^\s]+$", url):
        raise PurchaseError("The web address must start with http:// or https://")
    return {"name": _text(payload.get("name"), "The name", 80, True), "contact": _text(payload.get("contact"), "The contact", 200), "url": url, "notes": _text(payload.get("notes"), "The notes", 500)}


def clean_line(session: Session, payload: dict) -> dict:
    kind = payload.get("kind", "spool")
    if kind not in ("spool", "part"):
        raise PurchaseError("A line is a spool or a part")
    quantity = int(_num(payload.get("quantity", 1), "The quantity", 1, 10000, True))
    out = {"kind": kind, "quantity": quantity, "unit_cost": _num(payload.get("unit_cost"), "The price", 0, 1_000_000)}
    if kind == "spool":
        if quantity > MAX_SPOOLS_PER_LINE:
            raise PurchaseError(f"At most {MAX_SPOOLS_PER_LINE} spools on one line")
        material = _text(payload.get("material"), "The material", 40, True)
        brand, color = _text(payload.get("brand"), "The brand", 60), _text(payload.get("color"), "The colour", 60)
        colour_hex = _text(payload.get("color_hex"), "The colour code", 9)
        if colour_hex and not re.fullmatch(r"#?[0-9A-Fa-f]{6}", colour_hex):
            raise PurchaseError("The colour code is like #FF8800")
        out.update(material=material, brand=brand, color=color, color_hex=("#" + colour_hex.lstrip("#").upper()) if colour_hex else None,
                   weight_g=_num(payload.get("weight_g", 1000), "The weight", 10, 20000), description=" ".join(x for x in (brand, material, color) if x))
    else:
        inventory_id = payload.get("inventory_id")
        if isinstance(inventory_id, bool) or not isinstance(inventory_id, int) or not session.get(InventoryItem, inventory_id):
            raise PurchaseError("Choose the supply this part adds to (add it on the Supplies page first)")
        out.update(inventory_id=inventory_id, description=session.get(InventoryItem, inventory_id).name)
    return out


def total(session: Session, po: PurchaseOrder) -> float:
    return round(sum((l.unit_cost or 0) * l.quantity for l in session.exec(select(PurchaseLine).where(PurchaseLine.po_id == po.id)).all()), 2)


def as_json(session: Session, po: PurchaseOrder) -> dict:
    vendor = session.get(Vendor, po.vendor_id) if po.vendor_id else None
    lines = session.exec(select(PurchaseLine).where(PurchaseLine.po_id == po.id).order_by(PurchaseLine.id)).all()
    return {"id": po.id, "vendor_id": po.vendor_id, "vendor": vendor.name if vendor else None, "status": po.status, "reference": po.reference, "ordered_on": po.ordered_on,
            "expected_on": po.expected_on, "received_on": po.received_on, "notes": po.notes, "total": total(session, po), "lines": [l.model_dump() for l in lines]}


def receive(session: Session, po: PurchaseOrder, today: Optional[date] = None) -> dict:
    """Put what was bought in stock (once). Returns {"spools": n, "parts": n}."""
    if po.status == "received":
        raise PurchaseError("That order was already received")
    if po.status == "cancelled":
        raise PurchaseError("That order was cancelled")
    spools = parts = 0
    for line in session.exec(select(PurchaseLine).where(PurchaseLine.po_id == po.id)).all():
        if line.kind == "spool":
            weight = line.weight_g or 1000
            for _ in range(min(line.quantity, MAX_SPOOLS_PER_LINE)):
                session.add(Filament(material=line.material or "PLA", brand=line.brand, color=line.color, color_hex=line.color_hex, spool_weight_g=weight, remaining_g=weight, cost=line.unit_cost,
                                     notes=f"From purchase order {po.id}"))
                spools += 1
        else:
            item = session.get(InventoryItem, line.inventory_id) if line.inventory_id else None
            if item:
                item.quantity += line.quantity
                if line.unit_cost is not None:
                    item.unit_cost = line.unit_cost
                session.add(item)
                parts += line.quantity
    po.status, po.received_on = "received", (today or date.today()).isoformat()
    session.add(po)
    session.commit()
    return {"spools": spools, "parts": parts}


def buy_list(session: Session) -> list:
    """What looks worth buying: [{kind, description, reason, suggested, material, color...}]."""
    try:
        low_g = float(get_setting(session, "low_filament_g", "") or 100)
    except ValueError:
        low_g = 100.0
    out = []
    spools = session.exec(select(Filament)).all()
    groups: dict = {}
    for f in spools:
        key = ((f.material or "").lower(), (f.brand or "").lower(), (f.color or "").lower())
        g = groups.setdefault(key, {"material": f.material, "brand": f.brand, "color": f.color, "color_hex": f.color_hex, "left": 0.0, "count": 0, "weight": f.spool_weight_g or 1000})
        g["left"] += f.remaining_g or 0
        g["count"] += 1
    demand: dict = {}
    for q in session.exec(select(QueueItem).where(QueueItem.status.in_(["queued", "printing"]))).all():
        f = session.get(Filament, q.filament_id) if q.filament_id else None
        if f and q.estimated_grams:
            key = ((f.material or "").lower(), (f.brand or "").lower(), (f.color or "").lower())
            demand[key] = demand.get(key, 0.0) + q.estimated_grams
    for key, g in groups.items():
        needed = demand.get(key, 0.0)
        if g["left"] - needed < 0:
            short = needed - g["left"]
            out.append({"kind": "spool", "reason": f"the queue needs about {needed:.0f} g and only {g['left']:.0f} g is left", "suggested": max(1, math.ceil(short / g["weight"])), **{k: g[k] for k in ("material", "brand", "color", "color_hex")},
                        "weight_g": g["weight"], "description": " ".join(x for x in (g["brand"], g["material"], g["color"]) if x)})
        elif g["left"] <= low_g:
            out.append({"kind": "spool", "reason": f"only {g['left']:.0f} g is left (the warning is at {low_g:.0f} g)", "suggested": 1, **{k: g[k] for k in ("material", "brand", "color", "color_hex")},
                        "weight_g": g["weight"], "description": " ".join(x for x in (g["brand"], g["material"], g["color"]) if x)})
    for item in session.exec(select(InventoryItem)).all():
        if item.min_quantity and item.quantity <= item.min_quantity:
            out.append({"kind": "part", "inventory_id": item.id, "description": item.name, "reason": f"{item.quantity} left, the minimum is {item.min_quantity}", "suggested": max(1, item.min_quantity * 2 - item.quantity),
                        "unit_cost": item.unit_cost})
    return out
