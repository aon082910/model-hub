"""Follow designers: see their new uploads on the sites that let a server list them."""
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select

from app import sources
from app.db import get_session
from app.models import DesignerUpload, FollowedDesigner
from app.routers.discover import _in_library, _wishlisted

router = APIRouter(prefix="/api/designers", tags=["designers"])

MAX_FOLLOWED = 200
MAX_PER_CHECK = 50
KEEP_KNOWN = 300


def _known(designer: FollowedDesigner) -> Optional[list]:
    try:
        value = json.loads(designer.known_ids) if designer.known_ids else None
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def check_designer(session: Session, designer: FollowedDesigner, credentials: dict) -> int:
    """Look for new uploads. The first look only records what is already there. Returns how many are new.
    Raises SourceError if the site can't be asked (the designer's record is left as it was)."""
    uploads = sources.designer_uploads(designer.provider, designer.handle, credentials)
    known = _known(designer)
    ids = [u["source_id"] for u in uploads]
    new = []
    if known is not None:
        have = set(known)
        new = [u for u in uploads if u["source_id"] not in have]
        for u in reversed(new):         # oldest first, so ids stay in order of discovery
            session.add(DesignerUpload(designer_id=designer.id, provider=designer.provider, source_id=u["source_id"],
                                       title=u["title"][:300], thumbnail=u.get("thumbnail"), url=u.get("url"),
                                       license=u.get("license") or None))
    designer.known_ids = json.dumps((ids + (known or []))[:KEEP_KNOWN])
    designer.last_checked_at = datetime.utcnow()
    designer.last_error = None
    if uploads and not designer.name:
        designer.name = uploads[0].get("designer") or None
    session.add(designer)
    return len(new)


def _json(session: Session, designer: FollowedDesigner, counts: dict) -> dict:
    return {**designer.model_dump(exclude={"known_ids"}), "label": sources.PROVIDER_LABELS.get(designer.provider, designer.provider),
            "new_count": counts.get(designer.id, 0)}


def _unseen_counts(session: Session) -> dict:
    return dict(session.exec(select(DesignerUpload.designer_id, func.count()).where(DesignerUpload.seen == False)  # noqa: E712
                             .group_by(DesignerUpload.designer_id)).all())


@router.get("")
def list_designers(session: Session = Depends(get_session)):
    counts = _unseen_counts(session)
    rows = session.exec(select(FollowedDesigner).order_by(FollowedDesigner.name, FollowedDesigner.id)).all()
    return {"designers": [_json(session, d, counts) for d in rows], "new_total": sum(counts.values()),
            "providers": list(sources.FOLLOW_PROVIDERS)}


@router.post("")
def follow(payload: dict, session: Session = Depends(get_session)):
    provider, handle = payload.get("provider"), str(payload.get("handle") or "")
    if not sources.valid_designer_handle(provider, handle):
        raise HTTPException(400, "That designer cannot be followed from here")
    existing = session.exec(select(FollowedDesigner).where(
        FollowedDesigner.provider == provider, FollowedDesigner.handle == handle)).first()
    if existing:
        return _json(session, existing, _unseen_counts(session))
    if len(session.exec(select(FollowedDesigner.id)).all()) >= MAX_FOLLOWED:
        raise HTTPException(400, f"You can follow up to {MAX_FOLLOWED} designers")
    name = payload.get("name")
    designer = FollowedDesigner(provider=provider, handle=handle, name=name.strip()[:200] if isinstance(name, str) and name.strip() else None)
    session.add(designer)
    session.flush()
    try:
        check_designer(session, designer, sources.load_credentials(session))      # records what is already there
    except sources.SourceError as e:
        session.rollback()
        raise HTTPException(502, str(e))
    session.commit()
    session.refresh(designer)
    return _json(session, designer, {})


@router.delete("/{designer_id}")
def unfollow(designer_id: int, session: Session = Depends(get_session)):
    designer = session.get(FollowedDesigner, designer_id)
    if not designer:
        raise HTTPException(404, "Not found")
    for upload in session.exec(select(DesignerUpload).where(DesignerUpload.designer_id == designer_id)).all():
        session.delete(upload)
    session.delete(designer)
    session.commit()
    return {"status": "unfollowed"}


@router.post("/check")
def check_now(payload: dict, session: Session = Depends(get_session)):
    """Look for new uploads from everyone followed, or only those not looked at for stale_hours."""
    stale = payload.get("stale_hours")
    if stale is not None and (isinstance(stale, bool) or not isinstance(stale, (int, float)) or stale < 0):
        raise HTTPException(400, "stale_hours must be a number")
    designers = session.exec(select(FollowedDesigner).order_by(FollowedDesigner.last_checked_at)).all()
    if stale is not None:
        cutoff = datetime.utcnow() - timedelta(hours=stale)
        designers = [d for d in designers if not d.last_checked_at or d.last_checked_at < cutoff]
    credentials = sources.load_credentials(session)
    result = {"checked": 0, "new": 0, "errors": {}}
    for designer in designers[:MAX_PER_CHECK]:
        try:
            result["new"] += check_designer(session, designer, credentials)
            result["checked"] += 1
        except sources.SourceError as e:
            designer.last_error = str(e)[:300]
            session.add(designer)
            result["errors"][designer.name or designer.handle] = str(e)
        session.commit()
    result["new_total"] = sum(_unseen_counts(session).values())
    return result


@router.get("/uploads")
def list_uploads(unseen: bool = True, limit: int = Query(60, ge=1, le=200), session: Session = Depends(get_session)):
    stmt = select(DesignerUpload, FollowedDesigner).join(FollowedDesigner, FollowedDesigner.id == DesignerUpload.designer_id)
    if unseen:
        stmt = stmt.where(DesignerUpload.seen == False)  # noqa: E712
    rows = session.exec(stmt.order_by(DesignerUpload.id.desc()).limit(limit)).all()
    listings = [{"provider": u.provider, "source_id": u.source_id} for u, _ in rows]
    linked, saved = _in_library(session, listings), _wishlisted(session, listings)
    return [{**u.model_dump(), "designer": d.name or d.handle,
             "can_download": u.provider in sources.DOWNLOAD_PROVIDERS,
             "in_library": linked.get((u.provider, u.source_id)), "wishlist_id": saved.get((u.provider, u.source_id))}
            for u, d in rows]


@router.post("/uploads/seen")
def mark_seen(payload: dict, session: Session = Depends(get_session)):
    ids = payload.get("ids")
    if ids is None and payload.get("all") is not True:
        raise HTTPException(400, 'Give ids, or {"all": true}')
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise HTTPException(400, "ids must be a list of numbers")
    stmt = select(DesignerUpload).where(DesignerUpload.seen == False)  # noqa: E712
    if ids is not None:
        stmt = stmt.where(DesignerUpload.id.in_(ids))
    rows = session.exec(stmt).all()
    for u in rows:
        u.seen = True
        session.add(u)
    session.commit()
    return {"marked": len(rows)}
