"""The print log: what you printed, when, with what, how it turned out, and a photo."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlmodel import Session, select

from app.config import CONFIG_PATH
from app.db import get_session
from app import print_outcomes
from app.models import Filament, Model3D, PrintLog

router = APIRouter(prefix="/api/prints", tags=["prints"])

PHOTO_ROOT = CONFIG_PATH / "print_photos"
MAX_PHOTO_BYTES = 16 * 1024 * 1024


def photo_path(log_id: int):
    return PHOTO_ROOT / f"{log_id}.jpg"


def delete_photo(log_id: int) -> None:
    photo_path(log_id).unlink(missing_ok=True)


def _number(value, name: str, low: float, high: float) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be a number")
    if not (low <= number <= high):
        raise HTTPException(400, f"{name} must be between {low:g} and {high:g}")
    return number


def _when(value) -> datetime:
    if value in (None, ""):
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")[:19])
    except ValueError:
        raise HTTPException(400, "printed_at must be a date like 2026-10-08")


def _text(value, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, "notes must be text")
    return value.strip()[:limit] or None


def _take_filament(session: Session, log: PrintLog) -> None:
    """Subtract log.grams from its spool (never below zero) and remember what was actually taken."""
    log.deducted_g = 0
    if log.filament_id and log.grams:
        spool = session.get(Filament, log.filament_id)
        if spool:
            taken = min(float(log.grams), max(0.0, spool.remaining_g))
            spool.remaining_g = spool.remaining_g - taken
            log.deducted_g = taken
            session.add(spool)


def _give_back_filament(session: Session, log: PrintLog) -> None:
    if log.deducted_g and log.filament_id:
        spool = session.get(Filament, log.filament_id)
        if spool:
            spool.remaining_g = spool.remaining_g + log.deducted_g
            session.add(spool)
    log.deducted_g = 0


def _json(log: PrintLog, names: Optional[dict] = None) -> dict:
    return {**log.model_dump(), "has_photo": photo_path(log.id).is_file(),
            "model_filename": (names or {}).get(log.model_id), "failure_label": print_outcomes.label(log.failure_reason)}


def clean_reason(value) -> Optional[str]:
    """A failure reason key, or None for none. Anything else is refused."""
    if value in (None, ""):
        return None
    if value not in print_outcomes.REASONS:
        raise HTTPException(400, "reason must be one of: " + ", ".join(print_outcomes.REASONS))
    return value


def _names(session: Session, logs: list) -> dict:
    ids = {l.model_id for l in logs}
    if not ids:
        return {}
    return dict(session.exec(select(Model3D.id, Model3D.filename).where(Model3D.id.in_(ids))).all())


def log_print(session: Session, model_id: int, *, printed_at=None, filament_id=None, grams=None, minutes=None,
              rating=None, notes=None, deduct: bool = True, source: str = "manual", queue_item_id=None,
              measured: bool = False, outcome: Optional[str] = None, failure_reason: Optional[str] = None) -> PrintLog:
    """Record a print (does not commit). Used by the log's own API and by the print queue."""
    log = PrintLog(model_id=model_id, printed_at=printed_at or datetime.utcnow(), filament_id=filament_id,
                   grams=grams, minutes=minutes, rating=rating, notes=notes, source=source, queue_item_id=queue_item_id,
                   measured=bool(measured and minutes and outcome != "failed"),   # a failed print's time is only part of the job
                   outcome="failed" if outcome == "failed" else None, failure_reason=failure_reason if outcome == "failed" else None)
    session.add(log)
    session.flush()
    if deduct:
        _take_filament(session, log)
    return log


@router.get("/reasons")
def failure_reasons():
    """The reasons a print can be marked as failed with."""
    return [{"key": k, "label": v} for k, v in print_outcomes.REASONS.items()]


@router.get("")
def list_prints(model_id: Optional[int] = None, failed: Optional[bool] = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                session: Session = Depends(get_session)):
    stmt = select(PrintLog)
    count = select(func.count()).select_from(PrintLog)
    if model_id is not None:
        stmt = stmt.where(PrintLog.model_id == model_id)
        count = count.where(PrintLog.model_id == model_id)
    if failed is not None:
        condition = print_outcomes.failed() if failed else print_outcomes.ok()
        stmt, count = stmt.where(condition), count.where(condition)
    logs = session.exec(stmt.order_by(PrintLog.printed_at.desc(), PrintLog.id.desc()).offset(offset).limit(limit)).all()
    names = _names(session, logs)
    return {"total": session.exec(count).one(), "items": [_json(l, names) for l in logs]}


@router.post("")
def add_print(payload: dict, session: Session = Depends(get_session)):
    model = session.get(Model3D, payload.get("model_id")) if isinstance(payload.get("model_id"), int) else None
    if not model:
        raise HTTPException(404, "Model not found")
    filament_id = payload.get("filament_id")
    if filament_id is not None and not session.get(Filament, filament_id):
        raise HTTPException(400, "That filament spool does not exist")
    rating = _number(payload.get("rating"), "rating", 1, 5)
    minutes = _number(payload.get("minutes"), "minutes", 0, 10_000_000)
    outcome = payload.get("outcome")
    if outcome not in (None, "", "done", "failed"):
        raise HTTPException(400, "outcome must be done or failed")
    reason = clean_reason(payload.get("failure_reason")) if outcome == "failed" else None
    log = log_print(
        session, model.id, printed_at=_when(payload.get("printed_at")), filament_id=filament_id,
        grams=_number(payload.get("grams"), "grams", 0, 100000), minutes=minutes,
        rating=int(rating) if rating else None, notes=_text(payload.get("notes"), 4000),
        deduct=bool(payload.get("deduct", True)), measured=True, outcome=outcome, failure_reason=reason)
    session.commit()
    session.refresh(log)
    return _json(log, {model.id: model.filename})


@router.patch("/{log_id}")
def update_print(log_id: int, payload: dict, session: Session = Depends(get_session)):
    log = session.get(PrintLog, log_id)
    if not log:
        raise HTTPException(404, "Not found")
    if "printed_at" in payload:
        log.printed_at = _when(payload["printed_at"])
    if "rating" in payload:
        rating = _number(payload["rating"], "rating", 1, 5)
        log.rating = int(rating) if rating else None
    if "notes" in payload:
        log.notes = _text(payload["notes"], 4000)
    if "outcome" in payload:
        if payload["outcome"] not in (None, "", "done", "failed"):
            raise HTTPException(400, "outcome must be done or failed")
        log.outcome = "failed" if payload["outcome"] == "failed" else None
        if log.outcome is None:
            log.failure_reason = None
    if "failure_reason" in payload:
        reason = clean_reason(payload["failure_reason"])
        if reason and log.outcome != "failed":
            raise HTTPException(400, "Only a failed print has a failure reason")
        log.failure_reason = reason
    if "minutes" in payload:
        log.minutes = _number(payload["minutes"], "minutes", 0, 10_000_000)
    if "minutes" in payload or "outcome" in payload:
        log.measured = bool(log.minutes) and log.outcome != "failed"          # typed in by a person: a real time, unless it is a part-print
    if "grams" in payload or "filament_id" in payload:
        # the spool's level is kept right: give back what this print took, then take the new amount
        _give_back_filament(session, log)
        if "filament_id" in payload:
            if payload["filament_id"] is not None and not session.get(Filament, payload["filament_id"]):
                raise HTTPException(400, "That filament spool does not exist")
            log.filament_id = payload["filament_id"]
        if "grams" in payload:
            log.grams = _number(payload["grams"], "grams", 0, 100000)
        if payload.get("deduct", True):
            _take_filament(session, log)
    session.add(log)
    session.commit()
    session.refresh(log)
    return _json(log, _names(session, [log]))


@router.delete("/{log_id}")
def delete_print(log_id: int, session: Session = Depends(get_session)):
    log = session.get(PrintLog, log_id)
    if not log:
        raise HTTPException(404, "Not found")
    _give_back_filament(session, log)
    session.delete(log)
    session.commit()
    delete_photo(log_id)
    return {"status": "deleted"}


def store_photo(log_id: int, data: bytes) -> None:
    """Keep a picture (any common format, shrunk to a JPEG) as this print's photo. Raises SourceError for a bad picture."""
    from app.sources import shrink_image
    jpeg = shrink_image(data)
    PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
    photo_path(log_id).write_bytes(jpeg)


@router.post("/{log_id}/photo")
def set_photo(log_id: int, file: UploadFile = File(...), session: Session = Depends(get_session)):
    from app.sources import SourceError
    if not session.get(PrintLog, log_id):
        raise HTTPException(404, "Not found")
    data = file.file.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "That picture is larger than 16 MB")
    try:
        store_photo(log_id, data)
    except SourceError as e:
        raise HTTPException(400, str(e))
    return {"status": "saved"}


@router.get("/{log_id}/photo")
def get_photo(log_id: int):
    path = photo_path(log_id)
    if not path.is_file():
        raise HTTPException(404, "No photo")
    return FileResponse(path, media_type="image/jpeg")


@router.delete("/{log_id}/photo")
def remove_photo(log_id: int):
    delete_photo(log_id)
    return {"status": "deleted"}
