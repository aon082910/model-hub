"""Which spool is loaded in which slot of a printer (an AMS, an MMU, a toolchanger...).

A printer with slot_count N has slots 1..N. A queue entry can name the printer and a slot; the spool loaded in that slot at the
time is the one the print is counted against. A spool sits in at most one slot, so loading it somewhere takes it out of where it was.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import Filament, Printer, QueueItem, SpoolSlot

MAX_SLOTS = 16


def slot_count(printer: Optional[Printer]) -> int:
    return max(0, min(MAX_SLOTS, int(printer.slot_count or 0))) if printer else 0


def loaded(session: Session, printer_id: Optional[int], slot: Optional[int]) -> Optional[int]:
    """The id of the spool in that slot, or None."""
    if not printer_id or not slot:
        return None
    row = session.exec(select(SpoolSlot).where(SpoolSlot.printer_id == printer_id, SpoolSlot.slot == slot)).first()
    return row.filament_id if row else None


def effective_filament(session: Session, item: QueueItem) -> Optional[int]:
    """The spool a queue entry is printed from: the one in its slot right now, else the one chosen for the entry."""
    return loaded(session, item.printer_id, item.slot) or item.filament_id


def load(session: Session, printer: Printer, slot: int, filament_id: Optional[int], label: Optional[str] = None) -> SpoolSlot:
    """Put a spool in a slot (or empty it with None). Raises ValueError for a slot the printer does not have or an unknown spool."""
    if not 1 <= slot <= slot_count(printer):
        raise ValueError(f"{printer.name} has {slot_count(printer)} slot(s)" if slot_count(printer) else f"{printer.name} has no spool slots set up")
    if filament_id is not None and not session.get(Filament, filament_id):
        raise ValueError("That spool does not exist")
    if filament_id is not None:                          # a spool cannot be in two places
        for other in session.exec(select(SpoolSlot).where(SpoolSlot.filament_id == filament_id)).all():
            if (other.printer_id, other.slot) != (printer.id, slot):
                other.filament_id = None
                session.add(other)
    row = session.exec(select(SpoolSlot).where(SpoolSlot.printer_id == printer.id, SpoolSlot.slot == slot)).first() or SpoolSlot(printer_id=printer.id, slot=slot)
    row.filament_id = filament_id
    if label is not None:
        row.label = (label.strip()[:40] or None)
    row.loaded_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def overview(session: Session) -> list:
    """Every printer that has slots, with what is in each: [{id, name, slot_count, slots: [{slot, label, filament_id, spool}]}]."""
    spools = {f.id: f for f in session.exec(select(Filament)).all()}
    rows = {(r.printer_id, r.slot): r for r in session.exec(select(SpoolSlot)).all()}
    out = []
    for printer in session.exec(select(Printer).order_by(Printer.name)).all():
        count = slot_count(printer)
        if not count:
            continue
        slots = []
        for number in range(1, count + 1):
            row = rows.get((printer.id, number))
            spool = spools.get(row.filament_id) if row and row.filament_id else None
            slots.append({"slot": number, "label": row.label if row else None, "filament_id": spool.id if spool else None,
                          "spool": {"id": spool.id, "material": spool.material, "brand": spool.brand, "color": spool.color, "color_hex": spool.color_hex,
                                    "remaining_g": spool.remaining_g} if spool else None})
        out.append({"id": printer.id, "name": printer.name, "slot_count": count, "slots": slots})
    return out


def forget_printer(session: Session, printer_id: int) -> None:
    """A removed printer leaves no slots behind (SQLite reuses ids)."""
    for row in session.exec(select(SpoolSlot).where(SpoolSlot.printer_id == printer_id)).all():
        session.delete(row)


def forget_spool(session: Session, filament_id: int) -> None:
    for row in session.exec(select(SpoolSlot).where(SpoolSlot.filament_id == filament_id)).all():
        row.filament_id = None
        session.add(row)
