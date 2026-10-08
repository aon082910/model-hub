import csv
import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from app import downloads, sources
from app.db import get_session
from app.models import WishlistItem
from app.routers.discover import _in_library, queue_listings

router = APIRouter(prefix="/api/wishlist", tags=["wishlist"])

MAX_WISHLIST = 2000
STATUSES = ('wanted', 'got', 'skip')
MAX_LINKS_PER_IMPORT = 40
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _text(value, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, "values must be text")
    return value.strip()[:limit] or None


def _priority(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2):
        raise HTTPException(400, "priority must be 0 (low), 1 (normal) or 2 (high)")
    return value


def _status(value) -> str:
    if value not in STATUSES:
        raise HTTPException(400, "status must be one of: " + ", ".join(STATUSES))
    return value


def _json(item: WishlistItem, in_library: dict) -> dict:
    return {
        **item.model_dump(),
        "priority": 1 if item.priority is None else item.priority,
        "status": item.status or "wanted",
        "label": sources.PROVIDER_LABELS.get(item.provider, item.provider),
        "can_download": item.provider in sources.DOWNLOAD_PROVIDERS,
        "download_note": sources.DOWNLOAD_NOTES.get(item.provider),
        "in_library": in_library.get((item.provider, item.source_id)),
        "download": downloads.find_item(item.provider, item.source_id),
    }


@router.get("")
def list_wishlist(session: Session = Depends(get_session)):
    items = session.exec(select(WishlistItem)).all()
    # most wanted first, then newest
    items.sort(key=lambda i: (-(1 if i.priority is None else i.priority), -(i.created_at.timestamp()), -(i.id or 0)))
    linked = _in_library(session, [{"provider": i.provider, "source_id": i.source_id} for i in items])
    return [_json(i, linked) for i in items]


@router.get("/export.csv")
def export_wishlist(session: Session = Depends(get_session)):
    """The wishlist as a spreadsheet file (for a notes app, a friend, or a backup of the list)."""
    items = list_wishlist(session)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Site", "Title", "Designer", "License", "Link", "Priority", "Status", "Note", "In library"])
    names = {0: "low", 1: "normal", 2: "high"}
    for i in items:
        writer.writerow([i["label"], i["title"], i["designer"] or "", i["license"] or "", i["url"] or "",
                         names[i["priority"]], i["status"], i["note"] or "", "yes" if i["in_library"] else ""])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="wishlist.csv"'})


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
        priority=_priority(payload["priority"]) if "priority" in payload else 1,
        status=_status(payload["status"]) if "status" in payload else "wanted",
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
    if "priority" in payload:
        item.priority = _priority(payload["priority"])
    if "status" in payload:
        item.status = _status(payload["status"])
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
    if ids is None:     # "all" means everything still wanted; ones marked got/skip are left alone
        items = [i for i in items if (i.status or "wanted") == "wanted"]
    linked = _in_library(session, [{"provider": i.provider, "source_id": i.source_id} for i in items])
    items.sort(key=lambda i: -(1 if i.priority is None else i.priority))      # high priority first
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


def _room(session: Session) -> int:
    return MAX_WISHLIST - len(session.exec(select(WishlistItem.id)).all())


def _exists(session: Session, provider: str, source_id: str) -> bool:
    return session.exec(select(WishlistItem.id).where(
        WishlistItem.provider == provider, WishlistItem.source_id == source_id)).first() is not None


@router.post("/import-links")
def import_links(payload: dict, session: Session = Depends(get_session)):
    """Add every supported listing link found in some pasted text (a list of links, bookmarks, a chat...)."""
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "Paste some links first")
    found, seen, unrecognized = [], set(), 0
    for url in _URL.findall(text):
        parsed = sources.parse_url(url.rstrip(".,;"))
        if not parsed:
            unrecognized += 1
        elif parsed not in seen:
            seen.add(parsed)
            found.append(parsed)
    credentials = sources.load_credentials(session)
    result = {"added": 0, "already": 0, "failed": [], "unrecognized": unrecognized, "left_for_next_time": 0}
    room = _room(session)
    for index, (provider, source_id) in enumerate(found):
        if _exists(session, provider, source_id):
            result["already"] += 1
            continue
        if result["added"] >= MAX_LINKS_PER_IMPORT or result["added"] >= room:
            result["left_for_next_time"] = len(found) - index
            break
        try:
            details = sources.fetch_details(provider, source_id, credentials)
        except sources.SourceError as e:
            result["failed"].append({"provider": provider, "source_id": source_id, "reason": str(e)})
            continue
        session.add(WishlistItem(provider=provider, source_id=source_id, title=(details["title"] or source_id)[:300],
                                 thumbnail=(details.get("images") or [None])[0], url=details.get("url"),
                                 designer=details.get("designer") or None, license=details.get("license") or None))
        session.commit()
        result["added"] += 1
    return result


@router.post("/import-thingiverse")
def import_thingiverse(payload: dict, session: Session = Depends(get_session)):
    """Add the things you have liked on Thingiverse (kind "likes"), or a collection's things
    (kind "collection", ref = its link). Needs your Thingiverse token in Settings."""
    kind = payload.get("kind")
    if kind not in ("likes", "collection"):
        raise HTTPException(400, 'kind must be "likes" or "collection"')
    credentials = sources.load_credentials(session)
    if "thingiverse" not in credentials:
        raise HTTPException(400, "Add your Thingiverse token in Settings first")
    try:
        things = sources.thingiverse_import_list(kind, str(payload.get("ref") or ""), credentials)
    except sources.SourceError as e:
        raise HTTPException(502, str(e))
    added = already = 0
    room = _room(session)
    for t in things:
        if _exists(session, "thingiverse", t["source_id"]):
            already += 1
        elif added < room:
            session.add(WishlistItem(provider="thingiverse", source_id=t["source_id"], title=(t["title"] or t["source_id"])[:300],
                                     thumbnail=t.get("thumbnail"), url=t.get("url"), designer=t.get("designer") or None))
            added += 1
    session.commit()
    return {"found": len(things), "added": added, "already": already}
