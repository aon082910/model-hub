import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app import sources
from app.db import get_session
from app.models import Model3D, Tag
from app.source_match_jobs import clear_match_data

router = APIRouter(prefix="/api", tags=["sources"])
logger = logging.getLogger("modelhub.sources")

MAX_SITE_TAGS = 8


def _model_json(session: Session, model: Model3D) -> dict:
    from app.routers.library import _with_tags
    return _with_tags(session, [model])[0]


def _get_model(session: Session, model_id: int) -> Model3D:
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    return model


def _resolve_listing(payload: dict) -> tuple:
    """(provider, id) from a pasted URL or an explicit provider + source_id."""
    if payload.get("url"):
        parsed = sources.parse_url(payload["url"])
        if not parsed:
            raise HTTPException(400, "That is not a Printables or MakerWorld model link")
        return parsed
    provider, source_id = payload.get("provider"), str(payload.get("source_id") or "")
    if provider not in sources.PROVIDERS or not source_id.isdigit():
        raise HTTPException(400, "Give a model URL, or a provider and source_id")
    return provider, source_id


def link_model_to_listing(
    session: Session, model: Model3D, provider: str, source_id: str,
    images: bool = True, fill_details: bool = True, add_tags: bool = False,
) -> Model3D:
    """Fetch a listing and record it on the model. Network calls happen before
    any database change, so a failed lookup leaves the model untouched."""
    details = sources.fetch_details(provider, source_id)
    image_names = sources.store_images(model.id, details["images"]) if images else None

    model.source_provider = provider
    model.source_id = source_id
    model.source_url = details["url"]
    model.source_title = details["title"]
    model.source_description = details["description"] or None
    model.source_tags = json.dumps(details["tags"])
    model.source_synced_at = datetime.utcnow()
    if image_names is not None:
        model.source_images = json.dumps(image_names)
    if fill_details:
        if details["designer"]:
            model.designer = details["designer"]
        if details["license"]:
            model.license = details["license"]
    if add_tags:
        for raw in details["tags"][:MAX_SITE_TAGS]:
            name = raw.strip().lower()[:40]
            if not name:
                continue
            tag = session.exec(select(Tag).where(Tag.name == name)).first()
            if not tag:
                tag = Tag(name=name, ai_generated=False)
                session.add(tag)
                session.commit()
                session.refresh(tag)
            if tag not in model.tags:
                model.tags.append(tag)
    model.updated_at = datetime.utcnow()
    clear_match_data(session, model.id)   # decided: nothing left to review for this model
    session.add(model)
    session.commit()
    session.refresh(model)
    return model


def best_effort_link(session: Session, model: Model3D, source_url: Optional[str]) -> None:
    """Used after an extension import: fill in the listing details if the page
    it came from is a supported site. Never lets a site problem fail the import."""
    parsed = sources.parse_url(source_url or "")
    if not parsed:
        return
    try:
        link_model_to_listing(session, model, *parsed)
    except sources.SourceError as e:
        logger.info("Listing details not fetched for %s: %s", model.filename, e)
    except Exception:
        logger.exception("Listing details failed for %s", model.filename)
        session.rollback()


@router.get("/sources/search")
def search_sources(q: str, provider: Optional[str] = None):
    providers = (provider,) if provider in sources.PROVIDERS else sources.PROVIDERS
    try:
        found = sources.search(q, providers)
    except sources.SourceError as e:
        raise HTTPException(400, str(e))
    found["results"] = sources.rank(found["results"], q)
    return found


@router.get("/sources/lookup")
def lookup_source(url: str):
    parsed = sources.parse_url(url)
    if not parsed:
        raise HTTPException(400, "That is not a Printables or MakerWorld model link")
    try:
        return sources.fetch_details(*parsed)
    except sources.SourceError as e:
        raise HTTPException(502, str(e))


@router.get("/sources/parts")
def listing_parts(url: Optional[str] = None, provider: Optional[str] = None, source_id: Optional[str] = None):
    """The parts a listing says you need (MakerWorld), as rows ready to add to a project."""
    provider, source_id = _resolve_listing({"url": url, "provider": provider, "source_id": source_id})
    try:
        details = sources.fetch_details(provider, source_id)
    except sources.SourceError as e:
        raise HTTPException(502, str(e))
    note = ""
    if provider == "printables":
        note = "Printables listings don't have a parts list that can be read automatically."
    elif not details["parts"]:
        note = "This listing doesn't list any parts to buy."
    return {
        "listing": {"provider": provider, "source_id": source_id, "title": details["title"],
                    "url": details["url"], "designer": details["designer"]},
        "parts": details["parts"],
        "note": note,
    }


@router.get("/library/models/{model_id}/source/suggest")
def suggest_source(model_id: int, q: Optional[str] = None, session: Session = Depends(get_session)):
    model = _get_model(session, model_id)
    query = (q or "").strip() or sources.suggest_query(model.filename)
    if not query:
        return {"query": "", "results": [], "errors": {}}
    try:
        found = sources.search(query)
    except sources.SourceError as e:
        raise HTTPException(400, str(e))
    return {"query": query, "results": sources.rank(found["results"], query), "errors": found["errors"]}


@router.post("/library/models/{model_id}/source")
def link_source(model_id: int, payload: dict, session: Session = Depends(get_session)):
    model = _get_model(session, model_id)
    provider, source_id = _resolve_listing(payload)
    try:
        link_model_to_listing(
            session, model, provider, source_id,
            images=bool(payload.get("images", True)),
            fill_details=bool(payload.get("fill_details", True)),
            add_tags=bool(payload.get("add_tags", False)),
        )
    except sources.SourceError as e:
        raise HTTPException(502, str(e))
    return _model_json(session, model)


@router.delete("/library/models/{model_id}/source")
def unlink_source(model_id: int, session: Session = Depends(get_session)):
    model = _get_model(session, model_id)
    sources.delete_images(model.id)
    for field in ("source_provider", "source_id", "source_title", "source_description",
                  "source_tags", "source_images", "source_synced_at", "source_url"):
        setattr(model, field, None)
    session.add(model)
    session.commit()
    session.refresh(model)
    return _model_json(session, model)


@router.get("/library/models/{model_id}/source/images/{name}")
def source_image(model_id: int, name: str):
    path = sources.image_path(model_id, name)
    if not path:
        raise HTTPException(404, "Picture not found")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})
