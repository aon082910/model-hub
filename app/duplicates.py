"""Finding identical copies of a model in the library and cleaning them up.

Two files are duplicates when their content hash is the same: they are byte for byte
the same. Cleaning up keeps one, carries the others' tags, collections, projects,
notes, print log and listing link over to it, and (only when asked) deletes the
other files from the library folder. Before any file is deleted both files are hashed
again, so nothing but a real copy of the file you kept can ever be removed.

Files whose shape matches but whose bytes differ (a re-export, say) are listed as
"similar" for you to look at; they are never merged or deleted from here.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import func, update
from sqlmodel import Session, select

from app.config import LIBRARY_PATH, MODEL_EXTENSIONS
from app.models import (
    Model3D, ModelCollectionLink, ModelTagLink, PrintLog, ProjectModelFilament, ProjectModelLink, QueueItem,
)

SOURCE_FIELDS = ("source_provider", "source_id", "source_title", "source_description", "source_tags",
                 "source_synced_at", "source_filaments", "source_linked_by", "source_url")
FILL_FIELDS = ("designer", "license", "ai_description")
MAX_GROUPS = 200


class DuplicateError(Exception):
    """The request can't be carried out (and nothing was changed)."""


def _usage(session: Session, ids: list) -> dict:
    """How much of your own work is attached to each model: tags, collections, projects, prints."""
    def counts(table, column=None):
        column = column or table.model_id
        return dict(session.exec(select(column, func.count()).where(column.in_(ids)).group_by(column)).all())
    return {
        "tags": counts(ModelTagLink), "collections": counts(ModelCollectionLink), "projects": counts(ProjectModelLink),
        "prints": counts(PrintLog), "queue": counts(QueueItem),
    }


def _row(model: Model3D, usage: dict) -> dict:
    exists = (LIBRARY_PATH / model.path).is_file()
    linked = {k: usage[k].get(model.id, 0) for k in ("tags", "collections", "projects", "prints", "queue")}
    score = (sum(linked.values()) + (3 if model.source_provider else 0) + (1 if model.notes else 0)
             + (1 if model.designer else 0) + (1 if model.license else 0))
    return {"id": model.id, "filename": model.filename, "path": model.path, "size_bytes": model.size_bytes,
            "file_exists": exists, "thumbnail_path": model.thumbnail_path, "source_provider": model.source_provider,
            "has_notes": bool(model.notes), **linked, "score": score}


def _suggest(rows: list) -> int:
    """The copy most worth keeping: the most of your own work on it, then the one that exists, then the shortest path."""
    best = sorted(rows, key=lambda r: (-r["score"], not r["file_exists"], len(r["path"]), r["id"]))[0]
    return best["id"]


def find_groups(session: Session, offset: int = 0, limit: int = 50) -> dict:
    hashes = session.exec(
        select(Model3D.content_hash).where(Model3D.extension.in_(MODEL_EXTENSIONS))
        .group_by(Model3D.content_hash).having(func.count() > 1).order_by(Model3D.content_hash)).all()
    total = len(hashes)
    page = hashes[offset: offset + min(limit, MAX_GROUPS)]
    groups = []
    if page:
        models = session.exec(select(Model3D).where(Model3D.content_hash.in_(page)).order_by(Model3D.path)).all()
        usage = _usage(session, [m.id for m in models])
        by_hash = {}
        for m in models:
            by_hash.setdefault(m.content_hash, []).append(_row(m, usage))
        for h in page:
            rows = by_hash.get(h, [])
            groups.append({"hash": h, "models": rows, "suggested_keep": _suggest(rows),
                           "reclaimable_bytes": sum(r["size_bytes"] for r in rows) - max(r["size_bytes"] for r in rows)})
    extra = session.exec(select(func.coalesce(func.sum(Model3D.size_bytes), 0)).where(
        Model3D.extension.in_(MODEL_EXTENSIONS), Model3D.content_hash.in_(hashes))).one() if hashes else 0
    return {"total": total, "offset": offset, "groups": groups, "bytes_in_duplicate_files": extra}


def find_similar(session: Session, limit: int = 30) -> list:
    """Same shape (geometry hash), different bytes."""
    geo = session.exec(
        select(Model3D.geometry_hash).where(Model3D.geometry_hash.is_not(None), Model3D.extension.in_(MODEL_EXTENSIONS))
        .group_by(Model3D.geometry_hash).having(func.count(func.distinct(Model3D.content_hash)) > 1)
        .order_by(Model3D.geometry_hash).limit(limit)).all()
    out = []
    for g in geo:
        models = session.exec(select(Model3D).where(Model3D.geometry_hash == g).order_by(Model3D.path)).all()
        out.append([{"id": m.id, "filename": m.filename, "path": m.path, "size_bytes": m.size_bytes} for m in models])
    return out


def _carry_over(session: Session, keeper: Model3D, copy: Model3D, move: bool) -> None:
    """Give the kept model what the copy had that it does not."""
    have_tags = set(session.exec(select(ModelTagLink.tag_id).where(ModelTagLink.model_id == keeper.id)).all())
    for tag_id in session.exec(select(ModelTagLink.tag_id).where(ModelTagLink.model_id == copy.id)).all():
        if tag_id not in have_tags:
            session.add(ModelTagLink(model_id=keeper.id, tag_id=tag_id))
    have_cols = set(session.exec(select(ModelCollectionLink.collection_id).where(ModelCollectionLink.model_id == keeper.id)).all())
    for cid in session.exec(select(ModelCollectionLink.collection_id).where(ModelCollectionLink.model_id == copy.id)).all():
        if cid not in have_cols:
            session.add(ModelCollectionLink(model_id=keeper.id, collection_id=cid))
    have_projects = set(session.exec(select(ProjectModelLink.project_id).where(ProjectModelLink.model_id == keeper.id)).all())
    for pid in session.exec(select(ProjectModelLink.project_id).where(ProjectModelLink.model_id == copy.id)).all():
        if pid not in have_projects:
            session.add(ProjectModelLink(model_id=keeper.id, project_id=pid))
    for field in FILL_FIELDS:
        if not getattr(keeper, field) and getattr(copy, field):
            setattr(keeper, field, getattr(copy, field))
    if copy.notes and copy.notes not in (keeper.notes or ""):
        keeper.notes = f"{keeper.notes}\n\n{copy.notes}" if keeper.notes else copy.notes
    if not keeper.source_provider and copy.source_provider:
        for field in SOURCE_FIELDS:
            setattr(keeper, field, getattr(copy, field))
        from app.sources import copy_images
        keeper.source_images = None
        try:
            names = copy_images(copy.id, keeper.id)
            if names:
                import json
                keeper.source_images = json.dumps(names)
        except Exception:
            pass
    if move:        # what only makes sense to hand over when the copy is going away
        for table in (QueueItem, PrintLog, ProjectModelFilament):
            session.exec(update(table).where(table.model_id == copy.id).values(model_id=keeper.id))
    keeper.updated_at = datetime.utcnow()
    session.add(keeper)


def _identical_file(keeper: Model3D, copy: Model3D) -> Optional[str]:
    """None if the copy's file really is the same bytes as the kept file; otherwise why it can't be deleted."""
    from app.scanner import hash_file
    root = LIBRARY_PATH.resolve()
    keep_path, copy_path = (LIBRARY_PATH / keeper.path), (LIBRARY_PATH / copy.path)
    if not keep_path.is_file():
        return "the file you are keeping is missing, so nothing is deleted"
    if not copy_path.exists():
        return None                       # already gone: only the record is left to remove
    try:
        copy_resolved = copy_path.resolve()
        copy_resolved.relative_to(root)
    except (OSError, ValueError):
        return "that file is outside the library folder"
    if copy_resolved == keep_path.resolve() or not copy_path.is_file():
        return "that is the file you are keeping" if copy_resolved == keep_path.resolve() else "not a regular file"
    try:
        if hash_file(keep_path) != hash_file(copy_path):
            return "its contents have changed since the library was scanned"
    except OSError as e:
        return f"could not read it ({e.__class__.__name__})"
    return None


def merge(session: Session, keep_id: int, remove_ids: list, delete_files: bool) -> dict:
    """Keep `keep_id`; deal with the other exact copies. Without delete_files the copies stay and only
    their tags/collections/projects/notes are copied to the kept model."""
    keeper = session.get(Model3D, keep_id)
    if not keeper:
        raise DuplicateError("The model to keep was not found")
    ids = []
    for i in remove_ids:
        if not isinstance(i, int) or isinstance(i, bool):
            raise DuplicateError("remove_ids must be a list of model ids")
        if i != keep_id and i not in ids:
            ids.append(i)
    if not ids:
        raise DuplicateError("Choose at least one other copy")
    copies = session.exec(select(Model3D).where(Model3D.id.in_(ids))).all()
    if len(copies) != len(ids):
        raise DuplicateError("One of those models was not found")
    if any(c.content_hash != keeper.content_hash for c in copies):
        raise DuplicateError("Only exact copies (identical files) can be cleaned up together")

    result = {"merged": 0, "deleted_files": 0, "removed_records": 0, "freed_bytes": 0, "skipped": []}
    to_remove = []
    for copy in copies:
        reason = _identical_file(keeper, copy) if delete_files else None
        if reason:
            result["skipped"].append({"id": copy.id, "filename": copy.filename, "reason": reason})
            continue
        _carry_over(session, keeper, copy, move=delete_files)
        result["merged"] += 1
        if delete_files:
            path = LIBRARY_PATH / copy.path
            size = path.stat().st_size if path.exists() else 0
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                result["skipped"].append({"id": copy.id, "filename": copy.filename, "reason": f"could not delete it ({e.__class__.__name__})"})
                continue
            result["deleted_files"] += 1 if size else 0
            result["freed_bytes"] += size
            to_remove.append(copy.id)
    if to_remove:
        from app.library_cleanup import delete_model_records
        result["removed_records"] = delete_model_records(session, to_remove)
    session.commit()
    return result
