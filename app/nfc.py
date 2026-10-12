"""Spool tags (NFC): find, link and make spools from what a tag says.

What a tag may hold, in the forms that are open and documented:
* a Model Hub address (`https://host/#/spool/12`, what Model Hub itself writes) or the text `SPOOL:12`: the spool's own number;
* an **OpenSpool** record (JSON with `"protocol": "openspool"`: type, colour, brand, temperatures), which can make a new spool;
* anything else, like a tag's serial number: matched against the tag number kept on a spool.
A tag number belongs to one spool at a time: linking it to a spool takes it off any other. Reading and writing tags needs a phone with Web NFC (Chrome on Android), a USB reader or a
tag-writing app; Model Hub only handles the text it is given. Bambu, Creality and QIDI tags use closed formats and are not read."""
import json
import re
from typing import Optional

from sqlmodel import Session, select

from app.models import Filament

SPOOL_URL = re.compile(r"#/spool/(\d{1,9})\b")
SPOOL_TEXT = re.compile(r"^\s*SPOOL:(\d{1,9})\s*$", re.IGNORECASE)
HEX = re.compile(r"^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")
MAX_PAYLOAD = 4000


class NfcError(ValueError):
    """A tag that cannot be used; the message is safe to show."""


def _tag_id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{4,64}", value.strip()):
        raise NfcError("The tag number is 4 to 64 letters, numbers, colons or dashes")
    return value.strip().upper()


def parse(payload) -> dict:
    """What a tag's text means: {"spool_id": n} or {"spec": {...}} or {} when it says nothing Model Hub knows."""
    if not isinstance(payload, str) or not payload.strip():
        return {}
    text = payload.strip()[:MAX_PAYLOAD]
    m = SPOOL_URL.search(text) or SPOOL_TEXT.match(text)
    if m:
        return {"spool_id": int(m.group(1))}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        if isinstance(data, dict) and str(data.get("protocol", "")).lower() == "openspool":
            spec = {"material": str(data.get("type") or "").strip()[:40], "brand": str(data.get("brand") or "").strip()[:60] or None}
            colour = HEX.match(str(data.get("color_hex") or ""))
            spec["color_hex"] = "#" + colour.group(1).upper() if colour else None
            for key in ("min_temp", "max_temp"):
                value = data.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 500:
                    spec[key] = int(value)
            return {"spec": spec}
    return {}


def find(session: Session, tag: Optional[str]) -> Optional[Filament]:
    if not tag:
        return None
    return session.exec(select(Filament).where(Filament.nfc_tag == _tag_id(tag))).first()


def lookup(session: Session, payload=None, uid=None) -> dict:
    """{"match": spool or None, "parsed": {...}, "how": why it matched}. Matches by the number in the tag's text first, then by the tag's own number."""
    parsed = parse(payload)
    spool = None
    how = None
    if parsed.get("spool_id"):
        spool = session.get(Filament, parsed["spool_id"])
        how = "the spool number on the tag" if spool else None
    if not spool and uid:
        spool = find(session, uid)
        how = "the tag number" if spool else None
    return {"match": spool, "parsed": parsed, "how": how}


def claim(session: Session, uid, filament_id: int) -> dict:
    """Link a tag number to a spool (taking it off any other spool). Returns {"spool": ..., "moved_from": id or None}."""
    tag = _tag_id(uid)
    spool = session.get(Filament, filament_id)
    if not spool:
        raise NfcError("That spool does not exist")
    moved = None
    for other in session.exec(select(Filament).where(Filament.nfc_tag == tag, Filament.id != filament_id)).all():
        other.nfc_tag = None
        moved = other.id
        session.add(other)
    spool.nfc_tag = tag
    session.add(spool)
    session.commit()
    session.refresh(spool)
    return {"spool": spool, "moved_from": moved}


def create(session: Session, spec: dict, uid=None) -> Filament:
    """A new spool from an OpenSpool record (and link the tag number if one is given)."""
    if not isinstance(spec, dict) or not str(spec.get("material") or "").strip():
        raise NfcError("The tag does not say what material the spool is")
    tag = _tag_id(uid) if uid else None
    colour = spec.get("color_hex")
    spool = Filament(material=str(spec["material"]).strip()[:40], brand=spec.get("brand"), color_hex=colour if isinstance(colour, str) else None,
                     color=None, notes="Made from a spool tag")
    session.add(spool)
    session.commit()
    session.refresh(spool)
    if tag:
        claim(session, tag, spool.id)
        session.refresh(spool)
    return spool
