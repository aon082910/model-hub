"""Versions of the same model grouped into a family."""
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app import sources
from app.models import Model3D, ModelFamily


def members(session: Session, family_id: int) -> list:
    return session.exec(select(Model3D).where(Model3D.family_id == family_id).order_by(Model3D.created_at, Model3D.id)).all()


def family_summary(session: Session, model: Model3D) -> Optional[dict]:
    if not model.family_id:
        return None
    family = session.get(ModelFamily, model.family_id)
    if not family:
        return None
    return {"id": family.id, "name": family.name, "members": [
        {"id": m.id, "filename": m.filename, "version_label": m.version_label, "created_at": m.created_at, "size_bytes": m.size_bytes}
        for m in members(session, family.id)]}


def leave_families(session: Session, family_ids) -> None:
    """After models left the index: dissolve any family that is down to one model (or none). Does not commit."""
    for fid in {f for f in family_ids if f}:
        left = members(session, fid)
        if len(left) <= 1:
            for m in left:
                m.family_id, m.version_label = None, None
                session.add(m)
            family = session.get(ModelFamily, fid)
            if family:
                session.delete(family)


def group(session: Session, model_ids: list, name: Optional[str] = None) -> ModelFamily:
    """Put these models in one family (joining existing families together). Commits."""
    models = session.exec(select(Model3D).where(Model3D.id.in_(model_ids))).all()
    existing = sorted({m.family_id for m in models if m.family_id})
    family = session.get(ModelFamily, existing[0]) if existing else ModelFamily()
    if name:
        family.name = name
    session.add(family)
    session.flush()
    absorbed = set(existing[1:])
    for other in absorbed:
        for m in members(session, other):
            m.family_id = family.id
            session.add(m)
        gone = session.get(ModelFamily, other)
        if gone:
            session.delete(gone)
    for m in models:
        m.family_id = family.id
        session.add(m)
    session.commit()
    session.refresh(family)
    return family


def suggest(session: Session, model: Model3D, limit: int = 10) -> list:
    """Other models that look like versions of this one: the same shape, or the same name apart from a version suffix."""
    out, seen = [], {model.id}
    mine = sources.suggest_query(model.filename).lower()
    candidates = []
    if model.geometry_hash:
        candidates += session.exec(select(Model3D).where(Model3D.geometry_hash == model.geometry_hash, Model3D.id != model.id)).all()
    if mine:
        for other in session.exec(select(Model3D).where(Model3D.id != model.id, Model3D.filename.ilike(f"%{mine.split()[0]}%")).limit(300)).all():
            if sources.suggest_query(other.filename).lower() == mine:
                candidates.append(other)
    for other in candidates:
        if other.id in seen or (model.family_id and other.family_id == model.family_id):
            continue
        seen.add(other.id)
        out.append({"id": other.id, "filename": other.filename, "size_bytes": other.size_bytes, "in_family": other.family_id,
                    "same_shape": bool(model.geometry_hash and other.geometry_hash == model.geometry_hash)})
        if len(out) >= limit:
            break
    return out
