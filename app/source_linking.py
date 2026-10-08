"""Recording a site listing on a library model (shared by the API routes, the
extension import and the whole-library matching job)."""
import json
import logging
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app import sources
from app.models import Model3D, Tag
from app.source_match_jobs import clear_match_data

logger = logging.getLogger("modelhub.sources")

MAX_SITE_TAGS = 8
LINKED_BY = ("manual", "auto", "extension")


def link_model_to_listing(
    session: Session, model: Model3D, provider: str, source_id: str,
    images: bool = True, fill_details: bool = True, add_tags: bool = False, linked_by: str = "manual",
) -> Model3D:
    """Fetch a listing and record it on the model. Network calls happen before
    any database change, so a failed lookup leaves the model untouched."""
    details = sources.fetch_details(provider, source_id, sources.load_credentials(session))
    image_names = sources.store_images(model.id, details["images"]) if images else None

    model.source_provider = provider
    model.source_id = source_id
    model.source_url = details["url"]
    model.source_title = details["title"]
    model.source_description = details["description"] or None
    model.source_tags = json.dumps(details["tags"])
    model.source_filaments = json.dumps(details.get("filaments") or [])
    model.source_linked_by = linked_by if linked_by in LINKED_BY else "manual"
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


def unlink_model(session: Session, model: Model3D) -> Model3D:
    sources.delete_images(model.id)
    for field in ("source_provider", "source_id", "source_title", "source_description", "source_tags",
                  "source_images", "source_synced_at", "source_url", "source_filaments", "source_linked_by"):
        setattr(model, field, None)
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
        link_model_to_listing(session, model, *parsed, linked_by="extension")
    except sources.SourceError as e:
        logger.info("Listing details not fetched for %s: %s", model.filename, e)
    except Exception:
        logger.exception("Listing details failed for %s", model.filename)
        session.rollback()
