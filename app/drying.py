"""A reminder to dry spools. Filament takes up moisture from the air; how soon that matters depends on the material, so each has a
rule of thumb (days from opening or last drying). Only a spool that is loaded in a printer is nagged about."""
import json
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import Filament, SpoolSlot
from app.notify import notify_event
from app.settings_store import get_setting, set_setting

DEFAULT_DAYS = 90
EVERY_DAYS = {"PLA": 90, "PETG": 60, "ABS": 60, "ASA": 60, "TPU": 30, "PA": 7, "NYLON": 7, "PC": 14, "PVA": 3, "HIPS": 60, "PP": 30}
SOON = 0.85


def every_days(material: Optional[str]) -> int:
    key = str(material or "").upper().replace("-", "").replace(" ", "")
    for name, days in EVERY_DAYS.items():
        if key.startswith(name):
            return days
    return DEFAULT_DAYS


def describe(spool: Filament, now: Optional[datetime] = None) -> Optional[dict]:
    """How dry a spool is expected to be: None when nobody has said when it was opened or dried."""
    now = now or datetime.utcnow()
    marks = [d for d in (spool.opened_at, spool.dried_at) if d]
    if not marks:
        return None
    days_since = max(0, (now - max(marks)).days)
    limit = every_days(spool.material)
    return {"id": spool.id, "days_since": days_since, "every_days": limit, "days_left": limit - days_since,
            "status": "due" if days_since >= limit else "soon" if days_since >= limit * SOON else "ok",
            "dried_at": spool.dried_at.isoformat() if spool.dried_at else None, "opened_at": spool.opened_at.isoformat() if spool.opened_at else None}


def overview(session: Session) -> dict:
    return {s.id: d for s in session.exec(select(Filament)).all() if (d := describe(s))}


def loaded_ids(session: Session) -> set:
    return {r.filament_id for r in session.exec(select(SpoolSlot)).all() if r.filament_id}


def check_and_notify(session: Session) -> list:
    """Tell once about each spool in a printer that is due for drying (and again only after it was dried and became due once more)."""
    loaded = loaded_ids(session)
    due = {}
    for spool in session.exec(select(Filament)).all():
        d = describe(spool)
        if d and d["status"] == "due" and spool.id in loaded:
            due[spool.id] = f"{' '.join(x for x in (spool.material, spool.brand, spool.color) if x)} ({d['days_since']} days)"
    try:
        before = set(json.loads(get_setting(session, "dry_notified", "[]")))
    except ValueError:
        before = set()
    fresh = [text for key, text in due.items() if key not in before]
    set_setting(session, "dry_notified", json.dumps(sorted(due)))
    if fresh:
        notify_event(session, "dry_due", "Model Hub: spools to dry", "; ".join(fresh[:6]))
    return fresh
