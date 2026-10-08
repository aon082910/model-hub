"""The library's filters and sort orders, shared by the model list, saved searches and bulk edit."""
from typing import Optional

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from app.config import MODEL_EXTENSIONS
from app.models import (
    Model3D, ModelCollectionLink, ModelTagLink, PrintLog, ProjectModelLink, Tag,
)
from app.settings_store import get_setting

FILTER_KEYS = ("q", "tag", "extension", "duplicates_only", "printed", "designer", "license", "collection_id",
               "project_id", "linked", "fits_bed", "has_notes", "latest_only")
SORT_KEYS = ("id", "name", "newest", "oldest", "largest", "smallest", "last_printed")


class FilterError(ValueError):
    """A filter that cannot be applied (the message is for the person)."""


def bed_volume(session: Session) -> Optional[tuple]:
    """(x, y, z) of the printer's build volume in mm from Settings, or None when not set."""
    values = []
    for key in ("bed_x", "bed_y", "bed_z"):
        try:
            number = float(get_setting(session, key, "") or "")
        except ValueError:
            return None
        if number <= 0:
            return None
        values.append(number)
    return tuple(values)


def conditions(session: Session, f: dict) -> list:
    """SQL conditions for a dict of filters (keys in FILTER_KEYS; anything else is ignored)."""
    out = [Model3D.extension.in_(MODEL_EXTENSIONS)]       # anything that is not a model file stays out of the views

    def text(key):
        value = f.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    if text("extension"):
        out.append(Model3D.extension == text("extension"))
    if f.get("duplicates_only") is True:
        out.append(Model3D.is_duplicate_of.is_not(None))
    if f.get("printed") is True:
        out.append(Model3D.id.in_(select(PrintLog.model_id)))
    elif f.get("printed") is False:
        out.append(Model3D.id.not_in(select(PrintLog.model_id)))
    if f.get("linked") is True:
        out.append(Model3D.source_provider.is_not(None))
    elif f.get("linked") is False:
        out.append(Model3D.source_provider.is_(None))
    if f.get("latest_only") is True:        # only the newest file of each group of versions
        newest = select(func.max(Model3D.id)).where(Model3D.family_id.is_not(None)).group_by(Model3D.family_id)
        out.append(or_(Model3D.family_id.is_(None), Model3D.id.in_(newest)))
    if f.get("has_notes") is True:
        out.append(and_(Model3D.notes.is_not(None), Model3D.notes != ""))
    if text("q"):
        pattern = f"%{text('q')}%"
        out.append(or_(Model3D.filename.ilike(pattern), Model3D.ai_description.ilike(pattern)))
    if text("designer"):
        out.append(Model3D.designer.ilike(f"%{text('designer')}%"))
    if text("license"):
        out.append(Model3D.license.ilike(f"%{text('license')}%"))
    if text("tag"):
        tagged = select(ModelTagLink.model_id).join(Tag, ModelTagLink.tag_id == Tag.id).where(Tag.name.ilike(text("tag")))
        out.append(Model3D.id.in_(tagged))
    for key, link, column in (("collection_id", ModelCollectionLink, ModelCollectionLink.collection_id),
                              ("project_id", ProjectModelLink, ProjectModelLink.project_id)):
        value = f.get(key)
        if value not in (None, ""):
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise FilterError(f"{key} must be a number")
            out.append(Model3D.id.in_(select(link.model_id).where(column == number)))
    if f.get("fits_bed") is True:
        bed = bed_volume(session)
        if not bed:
            raise FilterError("Set your printer's build volume in Settings first (Settings, Print estimates)")
        bx, by, bz = bed
        known = and_(Model3D.bbox_x.is_not(None), Model3D.bbox_y.is_not(None), Model3D.bbox_z.is_not(None), Model3D.bbox_z <= bz)
        flat = or_(and_(Model3D.bbox_x <= bx, Model3D.bbox_y <= by), and_(Model3D.bbox_x <= by, Model3D.bbox_y <= bx))
        out.append(and_(known, flat))
    return out


def order_by(sort: Optional[str]):
    """The ORDER BY for a sort key, plus the outer join it needs (or None)."""
    sort = sort if sort in SORT_KEYS else "id"
    if sort == "name":
        return [func.lower(Model3D.filename), Model3D.id], None
    if sort == "newest":
        return [Model3D.created_at.desc(), Model3D.id.desc()], None
    if sort == "oldest":
        return [Model3D.created_at, Model3D.id], None
    if sort == "largest":
        return [Model3D.size_bytes.desc(), Model3D.id], None
    if sort == "smallest":
        return [Model3D.size_bytes, Model3D.id], None
    if sort == "last_printed":
        last = select(PrintLog.model_id, func.max(PrintLog.printed_at).label("last")).group_by(PrintLog.model_id).subquery()
        return [last.c.last.desc(), Model3D.id], last
    return [Model3D.id], None


def parse_flags(raw: dict) -> dict:
    """Turn query-string style values ('true', 'false', '') into the booleans conditions() expects."""
    out = dict(raw)
    for key in ("duplicates_only", "printed", "linked", "fits_bed", "has_notes", "latest_only"):
        value = out.get(key)
        if isinstance(value, str):
            out[key] = True if value == "true" else False if value == "false" else None
    return out
