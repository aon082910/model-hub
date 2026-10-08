"""Read-only share links: show a model, a project, a collection or the whole library to someone without giving them a login.

The link is a long random token. What a visitor sees is chosen here, not by the visitor: no notes, no
costs (unless asked for), no print history, nothing editable, and files only when downloads were
switched on for that link. Every piece of text is escaped, and the page is served with a locked-down
content policy so it cannot load or run anything else.
"""
import html
import json
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlmodel import Session, select

from app import activity, sources
from app.config import LIBRARY_PATH, THUMB_DIR
from app.db import get_session
from app.models import Collection, Filament, Model3D, ModelCollectionLink, Project, ProjectModelFilament, ProjectModelLink, ProjectPart, ShareLink

router = APIRouter(prefix="/api/shares", tags=["shares"])
public_router = APIRouter(prefix="/share", tags=["share-pages"], include_in_schema=False)

MAX_SHARES = 200
KINDS = ("model", "project", "collection", "library")
GALLERY_KINDS = ("project", "collection", "library")
PAGE_SIZE = 48
HEADERS = {
    "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'",
}


# ---------------------------------------------------------------- managing links

def _target_name(session: Session, link: ShareLink) -> Optional[str]:
    if link.kind == "model":
        m = session.get(Model3D, link.target_id)
        return m.filename if m else None
    if link.kind == "library":
        return "the whole library"
    if link.kind == "collection":
        c = session.get(Collection, link.target_id)
        return c.name if c else None
    p = session.get(Project, link.target_id)
    return p.name if p else None


def _json(session: Session, link: ShareLink) -> dict:
    return {"id": link.id, "kind": link.kind, "target_id": link.target_id, "target_name": _target_name(session, link),
            "path": f"/share/{link.token}", "allow_downloads": link.allow_downloads, "show_costs": link.show_costs,
            "created_at": link.created_at, "created_by": link.created_by, "expires_at": link.expires_at,
            "expired": bool(link.expires_at and link.expires_at < datetime.utcnow())}


@router.get("")
def list_shares(kind: Optional[str] = None, target_id: Optional[int] = None, session: Session = Depends(get_session)):
    stmt = select(ShareLink).order_by(ShareLink.created_at.desc())
    if kind:
        stmt = stmt.where(ShareLink.kind == kind)
    if target_id is not None:
        stmt = stmt.where(ShareLink.target_id == target_id)
    return [_json(session, l) for l in session.exec(stmt).all()]


@router.post("")
def create_share(payload: dict, request: Request, session: Session = Depends(get_session)):
    kind, target = payload.get("kind"), payload.get("target_id")
    if kind not in KINDS:
        raise HTTPException(400, "kind must be model, project, collection or library")
    user = getattr(request.state, "user", None) or {}
    if kind == "library":
        if user.get("role") != "admin":
            raise HTTPException(403, "Only the administrator can share the whole library")
        target = 0
    if not isinstance(target, int) or isinstance(target, bool):
        raise HTTPException(400, "target_id must be a number")
    found = {"model": Model3D, "project": Project, "collection": Collection}.get(kind)
    if found and not session.get(found, target):
        raise HTTPException(404, f"That {kind} was not found")
    days = payload.get("expires_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, (int, float)) or not (0 < days <= 3650)):
        raise HTTPException(400, "expires_days must be between 1 and 3650")
    if len(session.exec(select(ShareLink.id)).all()) >= MAX_SHARES:
        raise HTTPException(400, f"At most {MAX_SHARES} share links")
    link = ShareLink(token=secrets.token_urlsafe(24), kind=kind, target_id=target,
                     allow_downloads=payload.get("allow_downloads") is True, show_costs=payload.get("show_costs") is True,
                     created_by=user.get("username"), expires_at=datetime.utcnow() + timedelta(days=days) if days else None)
    session.add(link)
    session.commit()
    session.refresh(link)
    activity.record(session, activity.actor_of(request), "share", f"Made a share link for the {kind} {_target_name(session, link) or target}")
    return _json(session, link)


@router.delete("/{share_id}")
def revoke_share(share_id: int, request: Request, session: Session = Depends(get_session)):
    link = session.get(ShareLink, share_id)
    if not link:
        raise HTTPException(404, "Not found")
    what = f"{link.kind} {_target_name(session, link) or link.target_id}"
    session.delete(link)
    session.commit()
    activity.record(session, activity.actor_of(request), "share", f"Stopped sharing the {what}")
    return {"status": "revoked"}


# ---------------------------------------------------------------- the public pages

def _link(session: Session, token: str) -> ShareLink:
    link = session.exec(select(ShareLink).where(ShareLink.token == token)).first() if 20 <= len(token) <= 64 else None
    if not link or (link.expires_at and link.expires_at < datetime.utcnow()):
        raise HTTPException(404, "This link does not exist or has expired")
    return link


def _scope_ids(link: ShareLink):
    """A query for the ids of the models this link shows, or None when it is one fixed model (link.target_id)."""
    if link.kind == "project":
        return select(ProjectModelLink.model_id).where(ProjectModelLink.project_id == link.target_id)
    if link.kind == "collection":
        return select(ModelCollectionLink.model_id).where(ModelCollectionLink.collection_id == link.target_id)
    return None


def _scope_query(link: ShareLink):
    if link.kind == "model":
        return select(Model3D).where(Model3D.id == link.target_id)
    ids = _scope_ids(link)
    return select(Model3D) if ids is None else select(Model3D).where(Model3D.id.in_(ids))


def _models_in_scope(session: Session, link: ShareLink) -> list:
    return session.exec(_scope_query(link).order_by(Model3D.filename, Model3D.id)).all()


def _model_in_scope(session: Session, link: ShareLink, model_id: int) -> Optional[Model3D]:
    """One model, only if this link is meant to show it."""
    return session.exec(_scope_query(link).where(Model3D.id == model_id)).first()


def _gallery_page(session: Session, link: ShareLink, page: int):
    from sqlalchemy import func
    base = _scope_query(link)
    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    models = session.exec(base.order_by(Model3D.filename, Model3D.id).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return models, total


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _safe_url(url: Optional[str]) -> Optional[str]:
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def _size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>{title}</title><style>
body{{margin:0;background:#14161a;color:#e7e9ee;font-family:system-ui,sans-serif;line-height:1.45}}
main{{max-width:880px;margin:0 auto;padding:20px 16px 48px}} h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:17px;margin:22px 0 8px}}
.muted{{color:#9aa1ad;font-size:13px}} .card{{background:#1d2026;border:1px solid #2a2e37;border-radius:8px;padding:12px;margin:10px 0}}
.row{{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}} img{{max-width:100%;border-radius:6px;background:#0d0f12}}
.thumb{{width:150px;height:112px;object-fit:cover}} table{{border-collapse:collapse;width:100%}} td,th{{text-align:left;padding:5px 8px;border-bottom:1px solid #2a2e37;font-size:14px}}
.chip{{display:inline-block;border:1px solid #2a2e37;border-radius:10px;padding:0 8px;margin:0 4px 4px 0;font-size:12px;color:#9aa1ad}}
a{{color:#6aa5ff}} .badge{{background:#2a2e37;border-radius:4px;padding:1px 7px;font-size:12px}} pre{{white-space:pre-wrap;font-family:inherit;margin:0}}
</style></head><body><main>{body}<p class="muted">Shared from a self-hosted Model Hub. This page is read-only.</p></main></body></html>"""


def _model_card(session: Session, link: ShareLink, m: Model3D) -> str:
    thumb = f'<img class="thumb" src="/share/{_e(link.token)}/thumb/{m.id}" alt="" loading="lazy">'
    dims = (f"{m.bbox_x:.0f} x {m.bbox_y:.0f} x {m.bbox_z:.0f} mm" if None not in (m.bbox_x, m.bbox_y, m.bbox_z) else "")
    bits = [m.extension.lstrip("."), _size(m.size_bytes), dims, f"by {m.designer}" if m.designer else "", m.license or ""]
    download = (f' &middot; <a href="/share/{_e(link.token)}/file/{m.id}">Download file</a>' if link.allow_downloads else "")
    title = _e(m.source_title or m.filename)
    if link.kind in GALLERY_KINDS:
        title = f'<a href="/share/{_e(link.token)}/model/{m.id}">{title}</a>'
    return (f'<div class="card row">{thumb}<div><b>{title}</b><div class="muted">{_e(m.filename)}</div>'
            f'<div class="muted">{_e(" · ".join(b for b in bits if b))}{download}</div></div></div>')


def _render_model(session: Session, link: ShareLink, m: Model3D, tags: list) -> str:
    try:
        settings = json.loads(m.print_settings) if m.print_settings else {}
    except ValueError:
        settings = {}
    listing = _safe_url(m.source_url)
    body = f"<h1>{_e(m.source_title or m.filename)}</h1>"
    if m.source_title:
        body += f'<div class="muted">{_e(m.filename)}</div>'
    body += _model_card(session, link, m)
    if m.source_description:
        body += f"<h2>About</h2><div class=\"card\"><pre>{_e(m.source_description)}</pre></div>"
    if tags:
        body += "<h2>Tags</h2><div>" + "".join(f'<span class="chip">{_e(t)}</span>' for t in tags) + "</div>"
    if settings:
        names = {"material": "Material", "layer_height": "Layer height (mm)", "infill": "Infill", "supports": "Supports",
                 "nozzle_temp": "Nozzle", "bed_temp": "Bed", "speed": "Speed", "profile": "Slicer profile", "notes": "Notes"}
        rows = "".join(f"<tr><th>{_e(names.get(k, k))}</th><td>{_e(v)}</td></tr>" for k, v in settings.items())
        body += f"<h2>What worked</h2><div class=\"card\"><table>{rows}</table></div>"
    if listing:
        body += f'<h2>Original listing</h2><p><a href="{_e(listing)}" rel="noopener noreferrer nofollow">{_e(listing)}</a></p>'
    return PAGE.format(title=_e(m.source_title or m.filename), body=body)


def _render_project(session: Session, link: ShareLink, project: Project) -> str:
    models = _models_in_scope(session, link)
    body = f"<h1>{_e(project.name)}</h1><div class=\"muted\"><span class=\"badge\">{_e(project.status)}</span></div>"
    if project.description:
        body += f"<div class=\"card\"><pre>{_e(project.description)}</pre></div>"
    if models:
        body += "<h2>3D models</h2>" + "".join(_model_card(session, link, m) for m in models)
    lines = session.exec(select(ProjectModelFilament).where(ProjectModelFilament.project_id == project.id)).all()
    if lines:
        spools = {f.id: f for f in session.exec(select(Filament).where(Filament.id.in_({l.filament_id for l in lines}))).all()}
        totals = {}
        for l in lines:
            f = spools.get(l.filament_id)
            label = " ".join(x for x in (f.material, f.brand, f.color) if f) if f else "filament"
            totals[label] = totals.get(label, 0) + l.grams
        body += "<h2>Filament</h2><div class=\"card\"><table>" + "".join(
            f"<tr><td>{_e(k)}</td><td>{v:g} g</td></tr>" for k, v in totals.items()) + "</table></div>"
    parts = session.exec(select(ProjectPart).where(ProjectPart.project_id == project.id).order_by(ProjectPart.category, ProjectPart.name)).all()
    if parts:
        head = "<th>Part</th><th>Type</th><th>Qty</th>" + ("<th>Unit cost</th>" if link.show_costs else "")
        rows = "".join(f"<tr><td>{_e(p.name)}</td><td>{_e(p.category)}</td><td>{p.quantity}</td>"
                       + (f"<td>{'$%.2f' % p.unit_cost if p.unit_cost is not None else ''}</td>" if link.show_costs else "") + "</tr>"
                       for p in parts)
        body += f"<h2>Parts</h2><div class=\"card\"><table><tr>{head}</tr>{rows}</table></div>"
    return PAGE.format(title=_e(project.name), body=body)


def _render_gallery(session: Session, link: ShareLink, page: int) -> str:
    if link.kind == "collection":
        title = session.get(Collection, link.target_id).name
    else:
        title = "Model library"
    models, total = _gallery_page(session, link, page)
    pages = max(1, -(-total // PAGE_SIZE))
    body = f"<h1>{_e(title)}</h1><div class=\"muted\">{total} model{'' if total == 1 else 's'}</div>"
    body += "".join(_model_card(session, link, m) for m in models) or "<p class=\"muted\">Nothing here yet.</p>"
    if pages > 1:
        nav = []
        if page > 1:
            nav.append(f'<a href="/share/{_e(link.token)}?page={page - 1}">&larr; Previous</a>')
        nav.append(f"Page {page} of {pages}")
        if page < pages:
            nav.append(f'<a href="/share/{_e(link.token)}?page={page + 1}">Next &rarr;</a>')
        body += '<p class="muted">' + " &middot; ".join(nav) + "</p>"
    return PAGE.format(title=_e(title), body=body)


@public_router.get("/{token}/model/{model_id}", response_class=HTMLResponse)
def share_model_page(token: str, model_id: int, session: Session = Depends(get_session)):
    link = _link(session, token)
    model = _model_in_scope(session, link, model_id)
    if not model:
        raise HTTPException(404, "Not found")
    from app.routers.library import _with_tags
    page = _render_model(session, link, model, [t["name"] for t in _with_tags(session, [model])[0]["tags"]])
    if link.kind != "model":
        page = page.replace("<main>", f'<main><p><a href="/share/{_e(link.token)}">&larr; Back</a></p>', 1)
    return HTMLResponse(page, headers=HEADERS)


@public_router.get("/{token}", response_class=HTMLResponse)
def share_page(token: str, page: int = 1, session: Session = Depends(get_session)):
    link = _link(session, token)
    if link.kind in ("collection", "library"):
        if link.kind == "collection" and not session.get(Collection, link.target_id):
            raise HTTPException(404, "This link does not exist or has expired")
        return HTMLResponse(_render_gallery(session, link, max(1, min(page, 100000))), headers=HEADERS)
    if link.kind == "model":
        model = session.get(Model3D, link.target_id)
        if not model:
            raise HTTPException(404, "This link does not exist or has expired")
        from app.routers.library import _with_tags
        page = _render_model(session, link, model, [t["name"] for t in _with_tags(session, [model])[0]["tags"]])
    else:
        project = session.get(Project, link.target_id)
        if not project:
            raise HTTPException(404, "This link does not exist or has expired")
        page = _render_project(session, link, project)
    return HTMLResponse(page, headers=HEADERS)


@public_router.get("/{token}/thumb/{model_id}")
def share_thumb(token: str, model_id: int, session: Session = Depends(get_session)):
    link = _link(session, token)
    model = _model_in_scope(session, link, model_id)
    if not model:
        raise HTTPException(404, "Not found")
    if model.thumbnail_path and (THUMB_DIR / model.thumbnail_path).is_file():
        return FileResponse(THUMB_DIR / model.thumbnail_path, headers={"Cache-Control": "no-store"})
    try:
        names = json.loads(model.source_images or "[]")
    except ValueError:
        names = []
    picture = sources.image_path(model.id, names[0]) if names and isinstance(names[0], str) else None
    if picture:
        return FileResponse(picture, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    raise HTTPException(404, "No picture")


@public_router.get("/{token}/file/{model_id}")
def share_file(token: str, model_id: int, session: Session = Depends(get_session)):
    link = _link(session, token)
    if not link.allow_downloads:
        raise HTTPException(404, "Not found")
    model = _model_in_scope(session, link, model_id)
    path = (LIBRARY_PATH / model.path) if model else None
    if not model or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, filename=model.filename, headers={"Cache-Control": "no-store"})
