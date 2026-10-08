from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import func, or_
from sqlmodel import Session, select
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from app import library_filters
from app.db import get_session
from app.models import (
    Collection, Model3D, ModelCollectionLink, ModelTagLink, PrintLog, Project, ProjectModelLink, QueueItem, Tag,
)
from app.config import ARCHIVE_EXTENSIONS, LIBRARY_PATH, MODEL_EXTENSIONS, THUMB_DIR
from app.library_maintenance import LibraryMaintenanceBusy
from app.scanner import (
    ScanAlreadyRunning, import_uploaded_archive, import_uploaded_file, model_types_text, scan_library,
)
from app.library_cleanup import delete_model_records, non_model_summary, remove_non_models
from app.thumbnail_jobs import start_thumbnail_regeneration, thumbnail_regeneration_status
from app.ai.tagging import semantic_search
from app.estimate import estimate_print

router = APIRouter(prefix="/api/library", tags=["library"])


@router.post("/scan")
def trigger_scan(session: Session = Depends(get_session)):
    try:
        return scan_library(session)
    except ScanAlreadyRunning as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/thumbnails/regenerate")
def regenerate_thumbnails():
    """Start a background refresh of cached PNGs for already indexed mesh models."""
    try:
        return start_thumbnail_regeneration()
    except LibraryMaintenanceBusy as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/thumbnails/regenerate/status")
def thumbnail_regeneration_job_status():
    return thumbnail_regeneration_status()


@router.get("/models")
def list_models(
    response: Response,
    q: Optional[str] = None,
    tag: Optional[str] = None,
    extension: Optional[str] = None,
    duplicates_only: bool = False,
    printed: Optional[bool] = None,
    designer: Optional[str] = None,
    license: Optional[str] = None,
    collection_id: Optional[int] = None,
    project_id: Optional[int] = None,
    linked: Optional[bool] = None,
    fits_bed: bool = False,
    has_notes: bool = False,
    sort: Optional[str] = None,
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    """Return one page of models while applying search/filtering to the full library.

    The JSON response remains a plain list for backwards compatibility. Pagination
    metadata is returned through X-Total-Count, X-Limit, and X-Offset headers.
    """
    filters = {
        "q": q, "tag": tag, "extension": extension, "duplicates_only": duplicates_only or None, "printed": printed,
        "designer": designer, "license": license, "collection_id": collection_id, "project_id": project_id,
        "linked": linked, "fits_bed": fits_bed or None, "has_notes": has_notes or None,
    }
    try:
        conditions = library_filters.conditions(session, filters)
    except library_filters.FilterError as e:
        raise HTTPException(400, str(e))
    ordering, joined = library_filters.order_by(sort)

    count_stmt = select(func.count()).select_from(Model3D)
    stmt = select(Model3D)
    if joined is not None:
        stmt = stmt.outerjoin(joined, joined.c.model_id == Model3D.id)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        stmt = stmt.where(condition)

    total = session.exec(count_stmt).one()
    models = session.exec(stmt.order_by(*ordering).offset(offset).limit(limit)).all()

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    return _with_tags(session, models)


def _with_tags(session: Session, models: list) -> list:
    """Model rows as JSON dicts plus their tags (a relationship, so a plain
    row dump leaves it out). One query for the whole page. The packed
    embedding is left out: it's binary and nothing in the UI reads it."""
    tags_by_model = {}
    ids = [m.id for m in models]
    if ids:
        rows = session.exec(
            select(ModelTagLink.model_id, Tag)
            .join(Tag, ModelTagLink.tag_id == Tag.id)
            .where(ModelTagLink.model_id.in_(ids))
            .order_by(Tag.name)
        ).all()
        for model_id, tag in rows:
            tags_by_model.setdefault(model_id, []).append(
                {"id": tag.id, "name": tag.name, "ai_generated": tag.ai_generated})
    prints = {}
    if ids:
        for model_id, count, last in session.exec(
                select(PrintLog.model_id, func.count(PrintLog.id), func.max(PrintLog.printed_at))
                .where(PrintLog.model_id.in_(ids)).group_by(PrintLog.model_id)).all():
            prints[model_id] = (count, last)
    return [{**m.model_dump(exclude={"embedding"}), "tags": tags_by_model.get(m.id, []),
             "print_count": prints.get(m.id, (0, None))[0], "last_printed_at": prints.get(m.id, (0, None))[1]} for m in models]


@router.post("/import")
async def import_file(
    file: UploadFile = File(...),
    source_url: Optional[str] = Form(None),
    designer: Optional[str] = Form(None),
    license: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    """Receives a model file pushed in by the Model Hub browser extension
    (or any other client) and files it into the library. Only model files are
    accepted; a .zip is opened and just the model files inside are imported."""
    content = await file.read()
    ext = Path(file.filename or "").suffix.lower()
    from app.routers.sources import best_effort_link

    if ext in ARCHIVE_EXTENSIONS:
        try:
            result = import_uploaded_archive(
                session, file.filename, content,
                source_url=source_url, designer=designer, license=license,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        for model in result["models"]:
            await run_in_threadpool(best_effort_link, session, model, source_url)
        return {
            "imported": len(result["models"]), "ignored": result["ignored"],
            "models": [m.model_dump(exclude={"embedding"}) for m in result["models"]],
        }

    try:
        model = import_uploaded_file(
            session, file.filename or "", content,
            source_url=source_url, designer=designer, license=license,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # a model that came from a supported site gets its listing details (title,
    # pictures, tags...) filled in; a site problem never fails the import
    await run_in_threadpool(best_effort_link, session, model, source_url)
    return model.model_dump(exclude={"embedding"})


@router.get("/non-model-files")
def non_model_files(session: Session = Depends(get_session)):
    """Records in the index that are not model files (zips, notes...), left over
    from before the library was limited to model types."""
    return {**non_model_summary(session), "model_types": sorted(MODEL_EXTENSIONS)}


@router.post("/non-model-files/remove")
def remove_non_model_files(session: Session = Depends(get_session)):
    """Remove those records from the index. The files themselves are not touched."""
    return {"removed": remove_non_models(session)}


@router.get("/search/semantic")
def search_semantic(q: str, limit: int = 25, session: Session = Depends(get_session)):
    results = semantic_search(session, q, limit)
    return results


@router.get("/models/{model_id}")
def get_model(model_id: int, session: Session = Depends(get_session)):
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    return _with_tags(session, [model])[0]


@router.get("/models/{model_id}/full")
def get_model_full(model_id: int, session: Session = Depends(get_session)):
    """Everything the model's own page shows: the model with its tags, plus the
    projects, collections, print-queue entries and duplicates it is connected to."""
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    projects = session.exec(
        select(Project).join(ProjectModelLink, ProjectModelLink.project_id == Project.id)
        .where(ProjectModelLink.model_id == model_id).order_by(Project.name)).all()
    collections = session.exec(
        select(Collection).join(ModelCollectionLink, ModelCollectionLink.collection_id == Collection.id)
        .where(ModelCollectionLink.model_id == model_id).order_by(Collection.name)).all()
    queue = session.exec(select(QueueItem).where(QueueItem.model_id == model_id).order_by(QueueItem.position)).all()
    copies = session.exec(
        select(Model3D).where(Model3D.id != model_id, or_(
            Model3D.content_hash == model.content_hash, Model3D.is_duplicate_of == model_id,
            Model3D.id == (model.is_duplicate_of or -1)))
        .order_by(Model3D.path)).all()
    return {
        **_with_tags(session, [model])[0],
        "projects": [{"id": p.id, "name": p.name, "status": p.status} for p in projects],
        "collections": [{"id": c.id, "name": c.name} for c in collections],
        "queue": [{"id": q.id, "status": q.status, "position": q.position, "estimated_grams": q.estimated_grams,
                   "estimated_minutes": q.estimated_minutes} for q in queue],
        "duplicates": [{"id": m.id, "filename": m.filename, "path": m.path} for m in copies],
        "file_exists": (LIBRARY_PATH / model.path).exists(),
    }


PRINT_SETTING_FIELDS = {
    "material": 40, "layer_height": 20, "infill": 20, "supports": 40, "nozzle_temp": 20, "bed_temp": 20,
    "speed": 30, "profile": 120, "notes": 1000,
}


def parse_print_settings(model: Model3D) -> dict:
    try:
        data = json.loads(model.print_settings) if model.print_settings else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


@router.put("/models/{model_id}/print-settings")
def set_print_settings(model_id: int, payload: dict, session: Session = Depends(get_session)):
    """What worked for this model. Unknown fields are refused; empty ones are dropped."""
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    cleaned = {}
    for key, value in payload.items():
        if key not in PRINT_SETTING_FIELDS:
            raise HTTPException(400, f"Unknown print setting: {str(key)[:40]}")
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise HTTPException(400, f"{key} must be text or a number")
        text = str(value).strip()
        if len(text) > PRINT_SETTING_FIELDS[key]:
            raise HTTPException(400, f"{key} is too long")
        if text:
            cleaned[key] = text
    model.print_settings = json.dumps(cleaned) if cleaned else None
    model.updated_at = datetime.utcnow()
    session.add(model)
    session.commit()
    return {"print_settings": cleaned}


EDITABLE_TEXT_FIELDS = ("source_url", "designer", "license", "filename", "notes")


@router.patch("/models/{model_id}")
def update_model(model_id: int, payload: dict, session: Session = Depends(get_session)):
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    for field in EDITABLE_TEXT_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, f"{field} must be text")
        value = value.strip() if value else None
        if field == "filename" and not value:
            raise HTTPException(400, "filename cannot be empty")
        setattr(model, field, value)
    model.updated_at = datetime.utcnow()
    session.add(model)
    session.commit()
    session.refresh(model)
    return _with_tags(session, [model])[0]


@router.get("/models/{model_id}/file")
def download_model(model_id: int, session: Session = Depends(get_session)):
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    full_path = LIBRARY_PATH / model.path
    if not full_path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(full_path, filename=model.filename)


@router.get("/models/{model_id}/estimate")
def estimate_model_print(
    model_id: int, material: str = "PLA", infill: float = 0.15,
    session: Session = Depends(get_session),
):
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    full_path = LIBRARY_PATH / model.path
    if not full_path.exists():
        raise HTTPException(404, "File missing on disk")
    return estimate_print(model, full_path, material=material, infill=infill)


@router.get("/thumbnails/{filename}")
def get_thumbnail(filename: str):
    path = THUMB_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Thumbnail not found")
    return FileResponse(path)


@router.delete("/models/{model_id}")
def delete_model_record(model_id: int, session: Session = Depends(get_session)):
    """Removes the DB record only. Does not touch the file on disk."""
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    delete_model_records(session, [model_id])
    session.commit()
    return {"status": "deleted"}
