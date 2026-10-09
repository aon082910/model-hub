"""What a slicer sends to Model Hub's virtual printer.

PrusaSlicer, OrcaSlicer, Cura (with the OctoPrint plugin) and others can "upload" a sliced file to an OctoPrint or a Moonraker (Klipper) host. Model Hub answers as one of
those (app/routers/virtual_printer.py), keeps the file with the model it belongs to, and queues it if the slicer pressed "upload and print". It never starts a print: the
virtual printer has no machine behind it. A file it cannot place waits in the inbox until you say which model it is for (or, for a 3MF, make a new model from it)."""
import hashlib
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from app import print_files
from app.models import Model3D, PrintFile, QueueItem, SlicerUpload
from app.notify import notify_event
from app.settings_store import get_setting

MAX_WAITING = 200


class InboxError(ValueError):
    pass


def enabled(session: Session) -> bool:
    return get_setting(session, "virtual_printer", "") == "true"


def kind_of(filename: str) -> str:
    kind = Path(filename or "").suffix.lower().lstrip(".")
    if kind not in print_files.KINDS:
        raise InboxError("Only G-code (.gcode .gco .g .bgcode) or a sliced .3mf can be sent here")
    return kind


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def match_model(session: Session, filename: str) -> Optional[Model3D]:
    """The library model a sliced file is for: its name (without the slicer's settings added on) starts with the model's name, and exactly one model fits best."""
    stem = _norm(Path(filename).stem)
    if len(stem) < 3:
        return None
    candidates = []
    for m in session.exec(select(Model3D)).all():
        name = _norm(Path(m.filename).stem)
        if len(name) >= 3 and (stem == name or stem.startswith(name)):
            candidates.append((len(name), m))
    if not candidates:
        return None
    best = max(c[0] for c in candidates)
    top = [m for n, m in candidates if n == best]
    return top[0] if len(top) == 1 else None


def keep_for_model(session: Session, model: Model3D, stored_name: str, filename: str, kind: str, size: int, digest: str, note: Optional[str] = None) -> PrintFile:
    """File an already stored sliced file as one of the model's kept files (one copy per identical file)."""
    same = session.exec(select(PrintFile).where(PrintFile.model_id == model.id, PrintFile.sha256 == digest, PrintFile.printer_id.is_(None))).first()
    if same:
        print_files.stored_path(stored_name).unlink(missing_ok=True)
        return same
    meta = print_files.read_metadata(print_files.stored_path(stored_name), kind)
    row = PrintFile(model_id=model.id, filename=Path(filename).name[:200], stored_name=stored_name, kind=kind, size_bytes=size, sha256=digest,
                    notes=(note or "Sent from the slicer")[:1000], **meta)
    session.add(row)
    session.flush()
    return row


def queue_it(session: Session, model: Model3D, kept: PrintFile) -> QueueItem:
    top = session.exec(select(QueueItem).order_by(QueueItem.position.desc())).first()
    item = QueueItem(model_id=model.id, position=(top.position + 1) if top else 0, status="queued", estimated_grams=kept.est_grams, estimated_minutes=kept.est_minutes,
                     estimate_basis="estimate" if kept.est_minutes else None, notes="Sent from the slicer")
    session.add(item)
    session.flush()
    return item


def receive(session: Session, source: Path, filename: str, print_requested: bool, who: Optional[str]) -> SlicerUpload:
    """A file the slicer sent, already written to `source` (a temporary file). Files it, queues it, or leaves it waiting. Raises InboxError."""
    kind = kind_of(filename)
    size = source.stat().st_size
    if size == 0:
        raise InboxError("That file is empty")
    if len(session.exec(select(SlicerUpload.id).where(SlicerUpload.status == "waiting")).all()) >= MAX_WAITING:
        raise InboxError(f"{MAX_WAITING} files are already waiting: file or delete some first")
    print_files.PRINT_DIR.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.{kind}"
    shutil.move(str(source), str(print_files.stored_path(stored)))
    digest = _hash_file(print_files.stored_path(stored))
    row = SlicerUpload(filename=Path(filename).name[:200], stored_name=stored, kind=kind, size_bytes=size, sha256=digest, print_requested=print_requested, sent_by=who)
    session.add(row)
    session.flush()
    model = match_model(session, filename)
    if model:
        file_into(session, row, model)
    else:
        notify_event(session, "slicer_upload", "Model Hub: a file arrived from the slicer", f"{row.filename} is waiting in the inbox: say which model it is for.")
    session.commit()
    session.refresh(row)
    return row


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def file_into(session: Session, row: SlicerUpload, model: Model3D) -> PrintFile:
    """Keep the waiting file with this model (and queue it if the slicer asked to print). Does not commit."""
    kept = keep_for_model(session, model, row.stored_name, row.filename, row.kind, row.size_bytes, row.sha256)
    row.status, row.model_id, row.stored_name = "filed", model.id, None
    session.add(row)
    if row.print_requested:
        queue_it(session, model, kept)
    notify_event(session, "slicer_upload", "Model Hub: a file arrived from the slicer",
                 f"{row.filename} was filed with {model.filename}" + (" and queued" if row.print_requested else "") + ".")
    return kept
