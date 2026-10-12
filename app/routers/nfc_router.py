"""Spool tags: look a tag up, link it to a spool, or make a spool from an OpenSpool tag (see app.nfc)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from app import activity, nfc
from app.db import get_session

router = APIRouter(prefix="/api/nfc", tags=["nfc"])


def _spool(row) -> dict:
    return {"id": row.id, "material": row.material, "brand": row.brand, "color": row.color, "color_hex": row.color_hex, "remaining_g": row.remaining_g, "nfc_tag": row.nfc_tag}


@router.post("/lookup")
def lookup(payload: dict, session: Session = Depends(get_session)):
    """What a tag is: the spool it belongs to (by the number in its text, or its own number), and what its text says (a spool number, or an OpenSpool record that could make a spool)."""
    uid, text = payload.get("uid"), payload.get("payload")
    if uid is not None and not isinstance(uid, str):
        raise HTTPException(400, "uid must be text")
    if text is not None and not isinstance(text, str):
        raise HTTPException(400, "payload must be text")
    try:
        found = nfc.lookup(session, text, uid)
    except nfc.NfcError as e:
        raise HTTPException(400, str(e))
    return {"match": _spool(found["match"]) if found["match"] else None, "how": found["how"], "parsed": found["parsed"]}


@router.post("/claim")
def claim(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Link a tag number to a spool. A tag belongs to one spool: it is taken off any other."""
    filament_id = payload.get("filament_id")
    if isinstance(filament_id, bool) or not isinstance(filament_id, int):
        raise HTTPException(400, "filament_id must be a spool id")
    try:
        done = nfc.claim(session, payload.get("uid"), filament_id)
    except nfc.NfcError as e:
        raise HTTPException(400, str(e))
    activity.record(session, activity.actor_of(request), "filament", f"Linked a tag to spool {filament_id}" + (f" (taken off spool {done['moved_from']})" if done["moved_from"] else ""))
    return {"spool": _spool(done["spool"]), "moved_from": done["moved_from"]}


@router.post("/create")
def create(payload: dict, request: Request, session: Session = Depends(get_session)):
    """Make a new spool from an OpenSpool tag's record (spec, from /lookup) and link the tag's number to it."""
    try:
        spool = nfc.create(session, payload.get("spec"), payload.get("uid"))
    except nfc.NfcError as e:
        raise HTTPException(400, str(e))
    activity.record(session, activity.actor_of(request), "filament", f"Made a spool ({spool.material}) from a tag")
    return _spool(spool)
