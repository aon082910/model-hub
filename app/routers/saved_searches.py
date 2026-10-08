"""Library searches you want to come back to."""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import library_filters
from app.db import get_session
from app.models import SavedSearch

router = APIRouter(prefix="/api/saved-searches", tags=["saved-searches"])

MAX_SAVED = 100
ALLOWED = set(library_filters.FILTER_KEYS) | {"sort"}


def _clean(params) -> dict:
    if not isinstance(params, dict):
        raise HTTPException(400, "params must be the filters to save")
    out = {}
    for key, value in params.items():
        if key not in ALLOWED:
            raise HTTPException(400, f"Unknown filter: {str(key)[:40]}")
        if value in (None, "", False):
            continue
        if isinstance(value, bool) or (isinstance(value, (int, float)) and not isinstance(value, bool)):
            out[key] = value
        elif isinstance(value, str) and len(value) <= 200:
            out[key] = value
        else:
            raise HTTPException(400, f"{key} must be short text, a number or true/false")
    if "sort" in out and out["sort"] not in library_filters.SORT_KEYS:
        raise HTTPException(400, "Unknown sort order")
    return out


def _json(row: SavedSearch) -> dict:
    try:
        params = json.loads(row.params)
    except ValueError:
        params = {}
    return {"id": row.id, "name": row.name, "params": params, "created_at": row.created_at}


@router.get("")
def list_saved(session: Session = Depends(get_session)):
    return [_json(r) for r in session.exec(select(SavedSearch).order_by(SavedSearch.name)).all()]


@router.post("")
def save_search(payload: dict, session: Session = Depends(get_session)):
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "Give the search a name")
    params = _clean(payload.get("params"))
    if not params:
        raise HTTPException(400, "Set at least one filter first")
    if len(session.exec(select(SavedSearch.id)).all()) >= MAX_SAVED:
        raise HTTPException(400, f"At most {MAX_SAVED} saved searches")
    existing = session.exec(select(SavedSearch).where(SavedSearch.name == name.strip()[:80])).first()
    row = existing or SavedSearch(name=name.strip()[:80], params="{}")
    row.params = json.dumps(params)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _json(row)


@router.delete("/{search_id}")
def delete_saved(search_id: int, session: Session = Depends(get_session)):
    row = session.get(SavedSearch, search_id)
    if not row:
        raise HTTPException(404, "Not found")
    session.delete(row)
    session.commit()
    return {"status": "deleted"}
