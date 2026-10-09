from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app import print_outcomes
from app.db import get_session
from app.models import Model3D, PrintLog

router = APIRouter(prefix="/api/creators", tags=["creators"])


def _blank(name):
    return not name or not str(name).strip()


@router.get("")
def list_creators(q: str = "", session: Session = Depends(get_session)):
    """Everyone who is named as the designer of a model, with how many models they have here."""
    rows = session.exec(select(Model3D.designer, func.count(Model3D.id), func.sum(Model3D.size_bytes), func.max(Model3D.created_at))
                        .where(Model3D.designer.is_not(None), Model3D.designer != "").group_by(Model3D.designer)).all()
    needle = q.strip().lower()
    out = [{"name": name, "models": count, "size_bytes": int(size or 0), "latest": latest.isoformat() if latest else None}
           for name, count, size, latest in rows if not _blank(name) and needle in name.lower()]
    return {"creators": sorted(out, key=lambda c: (-c["models"], c["name"].lower()))[:2000]}


@router.get("/detail")
def creator(name: str, session: Session = Depends(get_session)):
    """One designer: their models here, how often they were printed, and the sites the models came from."""
    if _blank(name):
        raise HTTPException(400, "Give the designer's name")
    models = session.exec(select(Model3D).where(Model3D.designer == name).order_by(Model3D.filename)).all()
    if not models:
        raise HTTPException(404, "No model here names that designer")
    ids = [m.id for m in models]
    prints = dict(session.exec(select(PrintLog.model_id, func.count(PrintLog.id)).where(PrintLog.model_id.in_(ids), print_outcomes.ok()).group_by(PrintLog.model_id)).all())
    providers: dict = {}
    for m in models:
        if m.source_provider:
            providers[m.source_provider] = providers.get(m.source_provider, 0) + 1
    from app.routers.library import _with_tags
    return {"name": name, "models": _with_tags(session, models[:300]), "total": len(models), "printed": sum(1 for i in ids if prints.get(i)),
            "prints": sum(prints.values()), "size_bytes": sum(m.size_bytes or 0 for m in models),
            "providers": [{"provider": p, "models": n} for p, n in sorted(providers.items(), key=lambda kv: -kv[1])],
            "licenses": sorted({m.license for m in models if m.license})[:10]}
