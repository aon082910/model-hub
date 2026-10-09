from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app import fit
from app.db import get_session
from app.models import Model3D, Printer, QueueItem

router = APIRouter(prefix="/api/fit", tags=["fit"])


@router.get("/printers")
def printers(session: Session = Depends(get_session)):
    """The printers' names and bed sizes (nothing else about them), for choosing one in a list."""
    return [{"id": p.id, "name": p.name, "bed": [p.bed_x, p.bed_y, p.bed_z] if p.bed_x and p.bed_y else None}
            for p in session.exec(select(Printer).order_by(Printer.name)).all()]


@router.get("/queue")
def queue(session: Session = Depends(get_session)):
    """For waiting and running queue entries that name a printer with a bed size: whether the model fits it."""
    beds = {p.id: p for p in session.exec(select(Printer)).all()}
    out = {}
    for item in session.exec(select(QueueItem).where(QueueItem.status.in_(["queued", "printing"]), QueueItem.printer_id.is_not(None))).all():
        printer = beds.get(item.printer_id)
        model = session.get(Model3D, item.model_id)
        verdict = fit.fits(model, printer) if model else None
        if verdict is not None:
            out[item.id] = {"fits": verdict, "printer": printer.name, "model": [model.bbox_x, model.bbox_y, model.bbox_z],
                            "bed": [printer.bed_x, printer.bed_y, printer.bed_z]}
    return out


@router.get("/model/{model_id}")
def for_model(model_id: int, session: Session = Depends(get_session)):
    """Which printers (with a bed size) this model fits on and which it does not."""
    model = session.get(Model3D, model_id)
    if not model:
        return {"fits": [], "too_big": []}
    fitting, too_big = [], []
    for p in session.exec(select(Printer).order_by(Printer.name)).all():
        verdict = fit.fits(model, p)
        if verdict is not None:
            (fitting if verdict else too_big).append({"id": p.id, "name": p.name})
    return {"fits": fitting, "too_big": too_big}
