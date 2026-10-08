from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.db import get_session
from app.models import Model3D, QueueItem, Filament, PrintLog

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("")
def list_queue(session: Session = Depends(get_session)):
    items = session.exec(select(QueueItem).order_by(QueueItem.position)).all()
    return items


@router.post("")
def add_to_queue(payload: dict, session: Session = Depends(get_session)):
    max_pos = session.exec(select(QueueItem).order_by(QueueItem.position.desc())).first()
    position = (max_pos.position + 1) if max_pos else 0
    item = QueueItem(
        model_id=payload["model_id"],
        filament_id=payload.get("filament_id"),
        notes=payload.get("notes"),
        estimated_grams=payload.get("estimated_grams"),
        estimated_minutes=payload.get("estimated_minutes"),
        position=position,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _on_done(session: Session, item: QueueItem) -> None:
    """Deduct consumed filament exactly once, the moment a job transitions into "done",
    and write it to the model's print log (once per queue entry)."""
    if item.filament_id and item.estimated_grams:
        spool = session.get(Filament, item.filament_id)
        if spool:
            spool.remaining_g = max(0.0, spool.remaining_g - item.estimated_grams)
            session.add(spool)
    from app.routers.prints import log_print
    if not session.exec(select(PrintLog.id).where(PrintLog.queue_item_id == item.id)).first():
        log_print(session, item.model_id, filament_id=item.filament_id, grams=item.estimated_grams,
                  minutes=item.estimated_minutes, notes=item.notes, deduct=False, source="queue", queue_item_id=item.id)


def complete_item(session: Session, item: QueueItem, minutes: Optional[float] = None) -> None:
    """Mark a queue entry done (as a printer reporting a finished print does)."""
    if item.status == "done":
        return
    item.status = "done"
    if minutes and not item.estimated_minutes:
        item.estimated_minutes = round(minutes, 1)
    session.add(item)
    _on_done(session, item)


@router.post("/again/{model_id}")
def print_again(model_id: int, session: Session = Depends(get_session)):
    """Queue the model again with the filament, grams and time of its most recent print."""
    if not session.get(Model3D, model_id):
        raise HTTPException(404, "Model not found")
    last = session.exec(select(PrintLog).where(PrintLog.model_id == model_id)
                        .order_by(PrintLog.printed_at.desc(), PrintLog.id.desc())).first()
    top = session.exec(select(QueueItem).order_by(QueueItem.position.desc())).first()
    item = QueueItem(model_id=model_id, position=(top.position + 1) if top else 0,
                     filament_id=last.filament_id if last else None,
                     estimated_grams=last.grams if last else None, estimated_minutes=last.minutes if last else None,
                     notes="Printed again" if last else None)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.patch("/{item_id}")
def update_queue_item(item_id: int, payload: dict, session: Session = Depends(get_session)):
    item = session.get(QueueItem, item_id)
    if not item:
        raise HTTPException(404, "Not found")

    was_done = item.status == "done"
    for field in ("status", "position", "filament_id", "notes", "estimated_grams", "estimated_minutes"):
        if field in payload:
            setattr(item, field, payload[field])

    if item.status == "done" and not was_done:
        _on_done(session, item)

    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.delete("/{item_id}")
def remove_queue_item(item_id: int, session: Session = Depends(get_session)):
    item = session.get(QueueItem, item_id)
    if not item:
        raise HTTPException(404, "Not found")
    session.delete(item)
    session.commit()
    return {"status": "deleted"}
