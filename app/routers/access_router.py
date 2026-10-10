"""Access groups: named limits for logins (administrator only). A group only narrows what a login's role allows."""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app import activity
from app.auth import GROUP_AREAS
from app.db import get_session
from app.models import AccessGroup, AppUser, Printer

router = APIRouter(prefix="/api/access-groups", tags=["access"])
MAX_GROUPS = 30
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")


def _json(group: AccessGroup, members: int) -> dict:
    return {"id": group.id, "name": group.name, "deny": json.loads(group.deny_json or "[]"), "printers": json.loads(group.printers_json) if group.printers_json else None, "members": members}


def _clean(session: Session, payload: dict, existing=None) -> dict:
    out = {}
    if "name" in payload or existing is None:
        name = payload.get("name")
        if not isinstance(name, str) or not _NAME.match(name.strip()):
            raise HTTPException(400, "The name is 1 to 40 letters, numbers, spaces or . _ -")
        clash = session.exec(select(AccessGroup).where(AccessGroup.name == name.strip())).first()
        if clash and (existing is None or clash.id != existing.id):
            raise HTTPException(409, "A group has that name already")
        out["name"] = name.strip()
    if "deny" in payload:
        deny = payload["deny"]
        if not isinstance(deny, list) or any(a not in GROUP_AREAS for a in deny):
            raise HTTPException(400, "deny must be a list of: " + ", ".join(GROUP_AREAS))
        out["deny_json"] = json.dumps(sorted(set(deny)))
    if "printers" in payload:
        ids = payload["printers"]
        if ids in (None, []):
            out["printers_json"] = None
        else:
            if not isinstance(ids, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
                raise HTTPException(400, "printers must be a list of printer ids (or empty for every printer)")
            known = {p.id for p in session.exec(select(Printer)).all()}
            if any(i not in known for i in ids):
                raise HTTPException(400, "One of those printers does not exist")
            out["printers_json"] = json.dumps(sorted(set(ids)))
    return out


@router.get("")
def list_groups(session: Session = Depends(get_session)):
    counts = {}
    for u in session.exec(select(AppUser)).all():
        if u.group_id:
            counts[u.group_id] = counts.get(u.group_id, 0) + 1
    return {"groups": [_json(g, counts.get(g.id, 0)) for g in session.exec(select(AccessGroup).order_by(AccessGroup.name)).all()], "areas": GROUP_AREAS,
            "printers": [{"id": p.id, "name": p.name} for p in session.exec(select(Printer).order_by(Printer.name)).all()]}


@router.post("")
def create_group(payload: dict, request: Request, session: Session = Depends(get_session)):
    if len(session.exec(select(AccessGroup.id)).all()) >= MAX_GROUPS:
        raise HTTPException(400, f"At most {MAX_GROUPS} groups")
    group = AccessGroup(**_clean(session, payload))
    session.add(group)
    session.commit()
    session.refresh(group)
    activity.record(session, activity.actor_of(request), "user", f"Made the access group {group.name}")
    return _json(group, 0)


@router.patch("/{group_id}")
def update_group(group_id: int, payload: dict, request: Request, session: Session = Depends(get_session)):
    group = session.get(AccessGroup, group_id)
    if not group:
        raise HTTPException(404, "Not found")
    for key, value in _clean(session, payload, group).items():
        setattr(group, key, value)
    session.add(group)
    session.commit()
    session.refresh(group)
    activity.record(session, activity.actor_of(request), "user", f"Changed the access group {group.name}")
    return _json(group, len(session.exec(select(AppUser.id).where(AppUser.group_id == group.id)).all()))


@router.delete("/{group_id}")
def delete_group(group_id: int, request: Request, session: Session = Depends(get_session)):
    group = session.get(AccessGroup, group_id)
    if not group:
        raise HTTPException(404, "Not found")
    for u in session.exec(select(AppUser).where(AppUser.group_id == group_id)).all():
        u.group_id = None                       # ids are reused: a later group must not catch these logins
        session.add(u)
    name = group.name
    session.delete(group)
    session.commit()
    activity.record(session, activity.actor_of(request), "user", f"Removed the access group {name}")
    return {"status": "deleted"}
