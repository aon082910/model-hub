from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import downloads, sources
from app.db import get_session
from app.models import WishlistItem
from app.routers.discover import _in_library, queue_listings

router = APIRouter(prefix="/api/wishlist", tags=["wishlist"])

MAX_WISHLIST = 2000


def _text(value, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, "values must be text")
    return value.strip()[:limit] or None


def _json(item: WishlistItem, in_library: dict) -> dict:
    return {
        **item.model_dump(),
        "label": sources.PROVIDER_LABELS.get(item.provider, item.provider),
        "can_download": item.provider in sources.DOWNLOAD_PROVIDERS,
        "download_note": sources.DOWNLOAD_NOTES.get(item.provider),
        "in_library": in_library.get((item.provider, item.source_id)),
        "download": downloads.find_item(item.provider, item.source_id),
    }


@router.get("")
def list_wishlist(session: Session = Depends(get_session)):
    items = session.exec(select(WishlistItem).order_by(WishlistItem.created_at.desc(), WishlistItem.id.desc())).all()
    linked = _in_library(session, [{"provider": i.provider, "source_id": i.source_id} for i in items])
    return [_json(i, linked) for i in items]


@router.post("")
def add_to_wishlist(payload: dict, session: Session = Depends(get_session)):
    """Save a listing. Saving one that is already saved just returns it."""
    provider = payload.get("provider")
    source_id = str(payload.get("source_id") or "")
    if not sources.valid_source_id(provider, source_id):
        raise HTTPException(400, "Give a provider and the listing's id")
    existing = session.exec(select(WishlistItem).where(
        WishlistItem.provider == provider, WishlistItem.source_id == source_id)).first()
    if existing:
        return _json(existing, _in_library(session, [{"provider": provider, "source_id": source_id}]))
    if len(session.exec(select(WishlistItem.id)).all()) >= MAX_WISHLIST:
        raise HTTPException(400, f"The wishlist is full ({MAX_WISHLIST} items)")

    title = _text(payload.get("title"), 300)
    details = {}
    if not title:       # saved from somewhere that only knows the id: look the listing up
        try:
            details = sources.fetch_details(provider, source_id, sources.load_credentials(session))
        except sources.SourceError as e:
            raise HTTPException(502, str(e))
        title = details["title"] or source_id
    item = WishlistItem(
        provider=provider, source_id=source_id, title=title,
        thumbnail=_text(payload.get("thumbnail"), 1000) or ((details.get("images") or [None])[0]),
        url=_text(payload.get("url"), 1000) or details.get("url"),
        designer=_text(payload.get("designer"), 200) or details.get("designer") or None,
        license=_text(payload.get("license"), 200) or details.get("license") or None,
        note=_text(payload.get("note"), 2000),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return _json(item, _in_library(session, [{"provider": provider, "source_id": source_id}]))


@router.patch("/{item_id}")
def update_wishlist_item(item_id: int, payload: dict, session: Session = Depends(get_session)):
    item = session.get(WishlistItem, item_id)
    if not item:
        raise HTTPException(404, "Not found")
    if "note" in payload:
        item.note = _text(payload["note"], 2000)
    session.add(item)
    session.commit()
    session.refresh(item)
    return _json(item, _in_library(session, [{"provider": item.provider, "source_id": item.source_id}]))


@router.delete("/{item_id}")
def remove_from_wishlist(item_id: int, session: Session = Depends(get_session)):
    item = session.get(WishlistItem, item_id)
    if not item:
        raise HTTPException(404, "Not found")
    session.delete(item)
    session.commit()
    return {"status": "removed"}


@router.post("/download")
def download_wishlist(payload: dict, session: Session = Depends(get_session)):
    """Queue wishlist items for download: the given ids, or {"all": true} for every
    item that is not in the library yet. Sites this server cannot download from
    are reported with the reason."""
    ids = payload.get("ids")
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
        raise HTTPException(400, "ids must be a list of numbers")
    if ids is None and payload.get("all") is not True:
        raise HTTPException(400, 'Give ids, or {"all": true}')
    stmt = select(WishlistItem).order_by(WishlistItem.created_at, WishlistItem.id)
    items = session.exec(stmt.where(WishlistItem.id.in_(ids)) if ids is not None else stmt).all()
    linked = _in_library(session, [{"provider": i.provider, "source_id": i.source_id} for i in items])
    entries = [{"provider": i.provider, "source_id": i.source_id, "title": i.title, "thumbnail": i.thumbnail}
               for i in items if (i.provider, i.source_id) not in linked]
    if not entries:
        return {"added": [], "rejected": [], "already_in_library": len(items), **downloads.snapshot()}
    result = queue_listings(session, entries, images=bool(payload.get("images", True)),
                            add_tags=bool(payload.get("add_tags", False)))
    result["already_in_library"] = len(items) - len(entries)
    return result


@router.post("/remove-added")
def remove_added(session: Session = Depends(get_session)):
    """Take the items that are already in the library off the wishlist."""
    items = session.exec(select(WishlistItem)).all()
    linked = _in_library(session, [{"provider": i.provider, "source_id": i.source_id} for i in items])
    removed = 0
    for item in items:
        if (item.provider, item.source_id) in linked:
            session.delete(item)
            removed += 1
    session.commit()
    return {"removed": removed}
