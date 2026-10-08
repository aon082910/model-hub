"""Removing models from the library index cleanly.

Deleting a Model3D row alone leaves rows in the link tables pointing at it
(tags, collections, projects, queue, match candidates...), so everything that
removes models goes through here. Files on disk are never touched.
"""
from typing import Iterable

from sqlalchemy import delete, update
from sqlmodel import Session, select

from app.config import MODEL_EXTENSIONS
from app.models import (
    Model3D, ModelCollectionLink, ModelTagLink, ProjectModelFilament, ProjectModelLink, QueueItem,
    PrinterJob, PrintLog, SourceCandidate, SourceMatchState,
)


def delete_model_records(session: Session, model_ids: Iterable[int]) -> int:
    """Delete these models' index records and everything that points at them.
    Does not commit. Returns how many model rows were removed."""
    ids = list(model_ids)
    if not ids:
        return 0
    from app.routers.prints import delete_photo
    from app.sources import delete_images

    for log_id in session.exec(select(PrintLog.id).where(PrintLog.model_id.in_(ids))).all():
        delete_photo(log_id)
    for table in (PrinterJob, PrintLog, ModelTagLink, ModelCollectionLink, ProjectModelLink, ProjectModelFilament,
                  QueueItem, SourceCandidate, SourceMatchState):
        session.exec(delete(table).where(table.model_id.in_(ids)))
    # other rows that were marked as copies of a removed model are no longer copies
    session.exec(update(Model3D).where(Model3D.is_duplicate_of.in_(ids)).values(is_duplicate_of=None))
    removed = session.exec(delete(Model3D).where(Model3D.id.in_(ids))).rowcount
    for model_id in ids:
        delete_images(model_id)
    return removed


def non_model_summary(session: Session) -> dict:
    """What is in the index that is not a model file (left over from before
    the library was limited to model types)."""
    rows = session.exec(
        select(Model3D.extension, Model3D.filename).where(Model3D.extension.not_in(MODEL_EXTENSIONS))
    ).all()
    by_extension = {}
    for extension, _ in rows:
        by_extension[extension] = by_extension.get(extension, 0) + 1
    return {
        "count": len(rows),
        "by_extension": dict(sorted(by_extension.items(), key=lambda kv: -kv[1])),
        "examples": [name for _, name in rows[:5]],
    }


def remove_non_models(session: Session) -> int:
    ids = session.exec(select(Model3D.id).where(Model3D.extension.not_in(MODEL_EXTENSIONS))).all()
    removed = delete_model_records(session, ids)
    session.commit()
    return removed
