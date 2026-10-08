from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from app import sources
from app.db import get_session
from app.models import Model3D
from app.source_linking import best_effort_link, link_model_to_listing, unlink_model  # noqa: F401 (best_effort_link re-exported)

router = APIRouter(prefix="/api", tags=["sources"])

SITE_NAMES = "Printables, MakerWorld, Sketchfab or Thingiverse"


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
            raise HTTPException(400, f"That is not a {SITE_NAMES} model link")
        return parsed
    provider, source_id = payload.get("provider"), str(payload.get("source_id") or "")
    if not sources.valid_source_id(provider, source_id):
        raise HTTPException(400, "Give a model URL, or a provider and source_id")
    return provider, source_id.lower() if provider == "sketchfab" else source_id


@router.get("/sources/providers")
def list_providers(session: Session = Depends(get_session)):
    """The sites Model Hub can search, and whether each is usable right now
    (Thingiverse needs an access token in Settings)."""
    credentials = sources.load_credentials(session)
    return [{
        "id": p, "label": sources.PROVIDER_LABELS[p],
        "needs_token": p in sources.CREDENTIAL_SETTINGS,
        "enabled": p in sources.available_providers(credentials),
    } for p in sources.PROVIDERS]


@router.get("/sources/search")
def search_sources(q: str, provider: Optional[str] = None, session: Session = Depends(get_session)):
    credentials = sources.load_credentials(session)
    providers = (provider,) if provider in sources.PROVIDERS else None
    try:
        found = sources.search(q, providers, credentials=credentials)
    except sources.SourceError as e:
        raise HTTPException(400, str(e))
    found["results"] = sources.rank(found["results"], q)
    return found


@router.get("/sources/lookup")
def lookup_source(url: str, session: Session = Depends(get_session)):
    parsed = sources.parse_url(url)
    if not parsed:
        raise HTTPException(400, f"That is not a {SITE_NAMES} model link")
    try:
        return sources.fetch_details(*parsed, credentials=sources.load_credentials(session))
    except sources.SourceError as e:
        raise HTTPException(502, str(e))


@router.get("/sources/parts")
def listing_parts(url: Optional[str] = None, provider: Optional[str] = None, source_id: Optional[str] = None,
                  session: Session = Depends(get_session)):
    """The parts a listing says you need (MakerWorld), as rows ready to add to a project."""
    provider, source_id = _resolve_listing({"url": url, "provider": provider, "source_id": source_id})
    try:
        details = sources.fetch_details(provider, source_id, sources.load_credentials(session))
    except sources.SourceError as e:
        raise HTTPException(502, str(e))
    note = ""
    if provider != "makerworld":
        note = f"{sources.PROVIDER_LABELS[provider]} listings don't have a parts list that can be read automatically."
    elif not details["parts"]:
        note = "This listing doesn't list any parts to buy."
    return {
        "listing": {"provider": provider, "source_id": source_id, "title": details["title"],
                    "url": details["url"], "designer": details["designer"]},
        "parts": details["parts"],
        "note": note,
    }


@router.get("/library/models/{model_id}/source/suggest")
def suggest_source(model_id: int, q: Optional[str] = None, provider: Optional[str] = None,
                   session: Session = Depends(get_session)):
    model = _get_model(session, model_id)
    query = (q or "").strip() or sources.suggest_query(model.filename)
    if not query:
        return {"query": "", "results": [], "errors": {}}
    providers = (provider,) if provider in sources.PROVIDERS else None
    try:
        found = sources.search(query, providers, credentials=sources.load_credentials(session))
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
    return _model_json(session, unlink_model(session, _get_model(session, model_id)))


@router.get("/library/models/{model_id}/source/images/{name}")
def source_image(model_id: int, name: str):
    path = sources.image_path(model_id, name)
    if not path:
        raise HTTPException(404, "Picture not found")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})
