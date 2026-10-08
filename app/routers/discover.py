from collections import Counter
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlmodel import Session, select

from app import downloads, sources
from app.config import MODEL_EXTENSIONS
from app.db import get_session
from app.models import Model3D, ModelTagLink, Tag
from app.routers.library import _with_tags
from app.routers.sources import _resolve_listing

router = APIRouter(prefix="/api/discover", tags=["discover"])

MAX_QUEUE_BATCH = 100


def _library_search(session: Session, query: str, limit: int = 24) -> list:
    """Models already in the library that match the words anywhere that matters:
    file name, tags, descriptions, designer, listing title, your notes."""
    pattern = f"%{query.strip()}%"
    tagged = (select(ModelTagLink.model_id).join(Tag, ModelTagLink.tag_id == Tag.id).where(Tag.name.ilike(pattern)))
    stmt = (
        select(Model3D)
        .where(Model3D.extension.in_(MODEL_EXTENSIONS), or_(
            Model3D.filename.ilike(pattern), Model3D.ai_description.ilike(pattern), Model3D.source_title.ilike(pattern),
            Model3D.designer.ilike(pattern), Model3D.notes.ilike(pattern), Model3D.id.in_(tagged)))
        .order_by(Model3D.id.desc()).limit(limit)
    )
    return _with_tags(session, session.exec(stmt).all())


def _in_library(session: Session, listings: list) -> dict:
    """{(provider, source_id): model_id} for listings that are already linked to a library model."""
    if not listings:
        return {}
    ids = {str(l["source_id"]) for l in listings}
    providers = {l["provider"] for l in listings}
    rows = session.exec(select(Model3D.id, Model3D.source_provider, Model3D.source_id).where(
        Model3D.source_provider.in_(providers), Model3D.source_id.in_(ids))).all()
    found = {}
    for model_id, provider, source_id in rows:
        found.setdefault((provider, source_id), model_id)
    return found


@router.get("/search")
def discover_search(
    q: str,
    providers: Optional[str] = None,
    page: int = Query(1, ge=1, le=50),
    limit: int = Query(12, ge=1, le=30),
    library: bool = True,
    session: Session = Depends(get_session),
):
    """Search the library and every site at once. providers is a comma list
    (default: every site usable right now); page and limit apply per site."""
    query = (q or "").strip()
    if not query:
        raise HTTPException(400, "Type something to search for")
    credentials = sources.load_credentials(session)
    wanted = [p for p in (providers or "").split(",") if p in sources.PROVIDERS] or None
    searched = list(wanted or sources.available_providers(credentials))
    try:
        found = sources.search(query, wanted, limit=limit, credentials=credentials, page=page)
    except sources.SourceError as e:
        raise HTTPException(400, str(e))

    online = sources.rank(found["results"], query)
    linked = _in_library(session, online)
    per_site = Counter(r["provider"] for r in online)
    for r in online:
        r["can_download"] = r["provider"] in sources.DOWNLOAD_PROVIDERS
        r["in_library"] = linked.get((r["provider"], r["source_id"]))
    return {
        "query": query, "page": page, "limit": limit, "searched": searched,
        "library": _library_search(session, query) if library and page == 1 else [],
        "online": online, "errors": found["errors"],
        "has_more": any(per_site.get(p, 0) >= limit for p in searched),
    }


@router.get("/listing")
def discover_listing(
    url: Optional[str] = None, provider: Optional[str] = None, source_id: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """Everything about one listing for its preview page: the details, whether it
    is already in the library, and which files can be downloaded."""
    provider, source_id = _resolve_listing({"url": url, "provider": provider, "source_id": source_id})
    credentials = sources.load_credentials(session)
    try:
        details = sources.fetch_details(provider, source_id, credentials)
    except sources.SourceError as e:
        raise HTTPException(502, str(e))

    in_library = session.exec(select(Model3D.id, Model3D.filename).where(
        Model3D.source_provider == provider, Model3D.source_id == source_id)).all()
    can_download = provider in sources.DOWNLOAD_PROVIDERS
    files, files_note = None, None
    if can_download:
        try:
            files = downloads.list_files(provider, source_id, credentials)
            if not files:
                files_note = "This listing has no model files this server can download."
        except sources.SourceError as e:
            files_note = str(e)
    return {
        "details": details,
        "in_library": [{"id": i, "filename": n} for i, n in in_library],
        "can_download": can_download,
        "download_note": sources.DOWNLOAD_NOTES.get(provider),
        "files": files, "files_note": files_note,
        "download": downloads.find_item(provider, source_id),
    }


@router.post("/downloads")
def start_downloads(payload: dict, session: Session = Depends(get_session)):
    """Queue listings to be downloaded into the library. Items that can't be
    downloaded here are reported back with the reason rather than failing the batch."""
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "items must be a non-empty list")
    if len(items) > MAX_QUEUE_BATCH:
        raise HTTPException(400, f"at most {MAX_QUEUE_BATCH} at a time")
    credentials = sources.load_credentials(session)
    accepted, rejected = [], []
    for entry in items:
        provider = (entry or {}).get("provider")
        source_id = str((entry or {}).get("source_id") or "")
        if not sources.valid_source_id(provider, source_id):
            rejected.append({"provider": provider, "source_id": source_id, "reason": "Not a valid listing"})
        elif provider not in sources.DOWNLOAD_PROVIDERS:
            rejected.append({"provider": provider, "source_id": source_id, "reason": sources.DOWNLOAD_NOTES.get(provider, "Downloads are not available for this site")})
        elif provider not in credentials and provider in sources.CREDENTIAL_FIELDS:
            rejected.append({"provider": provider, "source_id": source_id,
                             "reason": f"Add your {sources.PROVIDER_LABELS[provider]} credentials in Settings first"})
        else:
            accepted.append({"provider": provider, "source_id": source_id,
                             "title": entry.get("title") or "", "thumbnail": entry.get("thumbnail")})
    added = downloads.enqueue(accepted, images=bool(payload.get("images", True)), add_tags=bool(payload.get("add_tags", False)))
    return {"added": added, "rejected": rejected, **downloads.snapshot()}


@router.get("/downloads")
def downloads_status():
    return downloads.snapshot()


@router.post("/downloads/clear")
def clear_downloads():
    return {"cleared": downloads.clear_finished()}


@router.delete("/downloads/{item_id}")
def cancel_download(item_id: str):
    if not downloads.cancel(item_id):
        raise HTTPException(404, "That download is not waiting or running")
    return {"status": "cancelling"}
