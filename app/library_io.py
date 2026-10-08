"""Exporting what Model Hub knows about your models, and reading it back (or reading a spreadsheet from another tool).

The export is about the *information* (tags, collections, projects, designer, license, notes, the listing a model came
from, print counts); the model files themselves stay in your library folder. Importing matches rows to models already
in the library (by content hash, then path, then a file name that is unique) and adds the information.
"""
import csv
import io
import json
from collections import defaultdict
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app import sources
from app.config import MODEL_EXTENSIONS
from app.models import (
    Collection, Model3D, ModelCollectionLink, ModelTagLink, PrintLog, Project, ProjectModelLink, Tag,
)

FORMAT = "modelhub-library"
VERSION = 1
CSV_COLUMNS = ["path", "filename", "tags", "collections", "projects", "designer", "license", "notes",
               "source_provider", "source_id", "source_url", "source_title", "print_count"]
MAX_IMPORT_BYTES = 100 * 1024 * 1024
MAX_TAGS_PER_MODEL = 50
MAX_PROBLEMS = 20
ALIASES = {
    "path": ("path", "file path", "filepath", "relative path"), "filename": ("filename", "file name", "file", "name"),
    "tags": ("tags", "tag", "labels"), "collections": ("collections", "collection", "folders", "categories"),
    "projects": ("projects", "project"), "designer": ("designer", "author", "creator"), "license": ("license", "licence"),
    "notes": ("notes", "note", "description", "comments"), "source_url": ("source_url", "url", "link", "source"),
    "source_provider": ("source_provider", "site"), "source_id": ("source_id",), "source_title": ("source_title",),
    "content_hash": ("content_hash", "hash", "sha256"),
}


class ImportError_(ValueError):
    """The file cannot be read as an export (the message is for the person)."""


# ---------------------------------------------------------------- export

def export_rows(session: Session) -> Iterable[dict]:
    models = session.exec(select(Model3D).where(Model3D.extension.in_(MODEL_EXTENSIONS)).order_by(Model3D.id)).all()
    tags, collections, projects = defaultdict(list), defaultdict(list), defaultdict(list)
    for mid, name in session.exec(select(ModelTagLink.model_id, Tag.name).join(Tag, ModelTagLink.tag_id == Tag.id).order_by(Tag.name)).all():
        tags[mid].append(name)
    for mid, name in session.exec(select(ModelCollectionLink.model_id, Collection.name)
                                  .join(Collection, ModelCollectionLink.collection_id == Collection.id).order_by(Collection.name)).all():
        collections[mid].append(name)
    for mid, name in session.exec(select(ProjectModelLink.model_id, Project.name)
                                  .join(Project, ProjectModelLink.project_id == Project.id).order_by(Project.name)).all():
        projects[mid].append(name)
    prints = {mid: (n, last) for mid, n, last in session.exec(
        select(PrintLog.model_id, func.count(PrintLog.id), func.max(PrintLog.printed_at)).group_by(PrintLog.model_id)).all()}
    for m in models:
        count, last = prints.get(m.id, (0, None))
        yield {
            "path": m.path, "filename": m.filename, "content_hash": m.content_hash, "extension": m.extension, "size_bytes": m.size_bytes,
            "tags": tags.get(m.id, []), "collections": collections.get(m.id, []), "projects": projects.get(m.id, []),
            "designer": m.designer, "license": m.license, "notes": m.notes,
            "source": {"provider": m.source_provider, "id": m.source_id, "url": m.source_url, "title": m.source_title} if m.source_provider else None,
            "print_count": count, "last_printed_at": last.isoformat() if last else None,
            "print_settings": json.loads(m.print_settings) if m.print_settings else None,
        }


def export_json(session: Session) -> str:
    rows = list(export_rows(session))
    return json.dumps({"format": FORMAT, "version": VERSION, "exported": datetime.utcnow().isoformat() + "Z", "models": rows},
                      ensure_ascii=False, default=str)


def _safe_cell(value) -> str:
    """Stop a spreadsheet from running a cell that starts with = + - @ as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(session: Session) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for r in export_rows(session):
        source = r["source"] or {}
        writer.writerow([_safe_cell(r["path"]), _safe_cell(r["filename"]), _safe_cell("; ".join(r["tags"])), _safe_cell("; ".join(r["collections"])),
                         _safe_cell("; ".join(r["projects"])), _safe_cell(r["designer"]), _safe_cell(r["license"]), _safe_cell(r["notes"]),
                         source.get("provider") or "", source.get("id") or "", _safe_cell(source.get("url")), _safe_cell(source.get("title")), r["print_count"]])
    return out.getvalue()


# ---------------------------------------------------------------- import

def _split(value) -> list:
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").replace(",", ";").split(";")
    return [str(i).strip() for i in items if str(i).strip()]


def _normalise_csv_row(raw: dict) -> dict:
    lowered = {str(k or "").strip().lower(): v for k, v in raw.items()}
    row = {}
    for canonical, names in ALIASES.items():
        for name in names:
            if lowered.get(name) not in (None, ""):
                row[canonical] = lowered[name]
                break
    return row


def parse_upload(data: bytes, filename: str) -> list:
    """Rows (dicts with the keys of ALIASES; tags/collections/projects as lists) from an export or a spreadsheet."""
    if len(data) > MAX_IMPORT_BYTES:
        raise ImportError_("That file is larger than 100 MB")
    text = data.decode("utf-8-sig", errors="replace")
    rows = []
    if filename.lower().endswith(".json") or text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError:
            raise ImportError_("That is not valid JSON")
        models = doc.get("models") if isinstance(doc, dict) else doc
        if not isinstance(models, list):
            raise ImportError_("That JSON has no list of models")
        if isinstance(doc, dict) and doc.get("format") not in (None, FORMAT):
            raise ImportError_("That JSON is not a Model Hub export")
        for m in models:
            if not isinstance(m, dict):
                continue
            row = {k: v for k, v in m.items() if k in ALIASES}
            source = m.get("source") if isinstance(m.get("source"), dict) else {}
            row.update({"source_provider": source.get("provider"), "source_id": source.get("id"),
                        "source_url": source.get("url"), "source_title": source.get("title")})
            rows.append(row)
    else:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ImportError_("That file has no header row")
        rows = [_normalise_csv_row(r) for r in reader]
    for row in rows:
        for key in ("tags", "collections", "projects"):
            row[key] = _split(row.get(key))
    return rows


def _find_model(row: dict, by_hash: dict, by_path: dict, by_name: dict) -> Optional[Model3D]:
    h = str(row.get("content_hash") or "").strip().lower()
    if h and h in by_hash:
        return by_hash[h]
    p = str(row.get("path") or "").strip().replace("\\", "/")
    if p and p in by_path:
        return by_path[p]
    name = str(row.get("filename") or "").strip().lower()
    if not name and p:
        name = p.rsplit("/", 1)[-1].lower()
    return by_name.get(name)


def apply_import(session: Session, rows: list, overwrite: bool = False, dry_run: bool = False) -> dict:
    models = session.exec(select(Model3D).where(Model3D.extension.in_(MODEL_EXTENSIONS))).all()
    by_hash = {m.content_hash.lower(): m for m in models if m.content_hash}
    by_path = {m.path.replace("\\", "/"): m for m in models}
    names = defaultdict(list)
    for m in models:
        names[m.filename.lower()].append(m)
    by_name = {n: ms[0] for n, ms in names.items() if len(ms) == 1}          # only unambiguous names
    tag_cache = {t.name: t for t in session.exec(select(Tag)).all()}
    col_cache = {c.name.lower(): c for c in session.exec(select(Collection)).all()}
    project_cache = {p.name.lower(): p for p in session.exec(select(Project)).all()}
    result = {"rows": len(rows), "matched": 0, "unmatched": 0, "models_changed": 0, "tags_added": 0, "collections_added": 0,
              "projects_added": 0, "fields_set": 0, "sources_set": 0, "created_tags": 0, "created_collections": 0,
              "problems": [], "dry_run": dry_run}
    created_cols, created_tags = set(), set()
    for number, row in enumerate(rows, start=2):
        model = _find_model(row, by_hash, by_path, by_name)
        if not model:
            result["unmatched"] += 1
            if len(result["problems"]) < MAX_PROBLEMS:
                result["problems"].append(f"Row {number}: no model matched ({row.get('path') or row.get('filename') or 'no name'})")
            continue
        result["matched"] += 1
        touched = False
        for field in ("designer", "license", "notes"):
            value = row.get(field)
            if isinstance(value, str) and value.strip() and (overwrite or not getattr(model, field)):
                value = value.strip()[:4000 if field == "notes" else 200]
                if getattr(model, field) != value:
                    result["fields_set"] += 1
                    touched = True
                    if not dry_run:
                        setattr(model, field, value)
                        session.add(model)
        have_tags = set(session.exec(select(ModelTagLink.tag_id).where(ModelTagLink.model_id == model.id)).all())
        for raw in row.get("tags", [])[:MAX_TAGS_PER_MODEL]:
            name = raw.lower()[:40]
            tag = tag_cache.get(name)
            if not tag:
                created_tags.add(name)
                if dry_run:
                    result["tags_added"] += 1
                    touched = True
                    continue
                tag = Tag(name=name, ai_generated=False)
                session.add(tag)
                session.flush()
                tag_cache[name] = tag
            if tag.id not in have_tags:
                result["tags_added"] += 1
                touched = True
                if not dry_run:
                    session.add(ModelTagLink(model_id=model.id, tag_id=tag.id))
        have_cols = set(session.exec(select(ModelCollectionLink.collection_id).where(ModelCollectionLink.model_id == model.id)).all())
        for raw in row.get("collections", [])[:20]:
            name = raw[:80]
            col = col_cache.get(name.lower())
            if not col:
                created_cols.add(name.lower())
                if dry_run:
                    result["collections_added"] += 1
                    touched = True
                    continue
                col = Collection(name=name)
                session.add(col)
                session.flush()
                col_cache[name.lower()] = col
            if col.id not in have_cols:
                result["collections_added"] += 1
                touched = True
                if not dry_run:
                    session.add(ModelCollectionLink(model_id=model.id, collection_id=col.id))
        have_projects = set(session.exec(select(ProjectModelLink.project_id).where(ProjectModelLink.model_id == model.id)).all())
        for raw in row.get("projects", [])[:20]:
            project = project_cache.get(raw.lower())          # projects are never created from an import: they hold parts and filament
            if project and project.id not in have_projects:
                result["projects_added"] += 1
                touched = True
                if not dry_run:
                    session.add(ProjectModelLink(model_id=model.id, project_id=project.id))
        provider, source_id = str(row.get("source_provider") or "").strip().lower(), str(row.get("source_id") or "").strip()
        if provider and not model.source_provider and sources.valid_source_id(provider, source_id):
            result["sources_set"] += 1
            touched = True
            if not dry_run:
                url = str(row.get("source_url") or "").strip()
                model.source_provider, model.source_id = provider, source_id
                model.source_url = url if url.startswith(("http://", "https://")) else model.source_url
                model.source_title = (str(row.get("source_title") or "").strip()[:300]) or None
                model.source_linked_by = "manual"
                session.add(model)
        if touched:
            result["models_changed"] += 1
    result["created_tags"], result["created_collections"] = len(created_tags), len(created_cols)
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return result
