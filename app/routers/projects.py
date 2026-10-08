import csv
import io
import json
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select
from app.db import get_session
from app.estimate import minutes_for_grams
from app.filament_match import match_spool, spool_label
from app.settings_store import get_setting
from app.models import Filament, InventoryItem, Model3D, Project, ProjectModelFilament, ProjectModelLink, ProjectPart

router = APIRouter(prefix="/api/projects", tags=["projects"])

PART_CATEGORIES = ("electronics", "parts", "supplies")
PROJECT_STATUSES = ("planning", "building", "printed", "done")
# reaching either of these means the prints are finished and filament is spent
PRINTED_STATUSES = ("printed", "done")


def _clean_text(value, field: str, required: bool = False) -> Optional[str]:
    if value is None:
        if required:
            raise HTTPException(400, f"{field} is required")
        return None
    if not isinstance(value, str):
        raise HTTPException(400, f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise HTTPException(400, f"{field} is required")
    return value or None


def _clean_int(value, field: str, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a whole number")
    if number < minimum:
        raise HTTPException(400, f"{field} must be at least {minimum}")
    return number


def _clean_cost(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "unit_cost must be a number")
    if cost < 0:
        raise HTTPException(400, "unit_cost cannot be negative")
    return cost


def _clean_choice(value, field: str, allowed) -> str:
    if value not in allowed:
        raise HTTPException(400, f"{field} must be one of: {', '.join(allowed)}")
    return value


def _part_json(part: ProjectPart) -> dict:
    needed = max(0, part.quantity - part.quantity_owned)
    return {
        **part.model_dump(),
        "quantity_needed": needed,
        "cost_needed": round(needed * part.unit_cost, 2) if part.unit_cost is not None else None,
    }


def _clean_grams(value) -> float:
    try:
        grams = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "grams must be a number")
    if grams < 0:
        raise HTTPException(400, "grams cannot be negative")
    return round(grams, 2)


def _filament_label(spool: Optional[Filament]) -> str:
    if not spool:
        return "(deleted spool)"
    return " ".join(x for x in (spool.material, spool.brand, spool.color) if x)


def _filament_line_json(line: ProjectModelFilament, spool: Optional[Filament]) -> dict:
    return {
        **line.model_dump(),
        "filament_label": _filament_label(spool),
        "color_hex": spool.color_hex if spool else None,
        "remaining_g": spool.remaining_g if spool else None,
    }


def _reject_if_deducted(project: Project):
    if project.filament_deducted:
        raise HTTPException(
            409, "Filament was already subtracted for this project; move it back to planning/building before changing filament")


def _apply_filament_status(session: Session, project: Project):
    """Subtract the project's filament from inventory the moment it becomes
    printed/done, exactly once; give back what was taken if it is moved back."""
    lines = session.exec(select(ProjectModelFilament).where(ProjectModelFilament.project_id == project.id)).all()
    if project.status in PRINTED_STATUSES and not project.filament_deducted:
        for line in lines:
            spool = session.get(Filament, line.filament_id)
            if not spool:
                continue
            taken = min(line.grams, spool.remaining_g)
            spool.remaining_g = round(spool.remaining_g - taken, 2)
            line.deducted_g = taken
            session.add(spool)
            session.add(line)
        project.filament_deducted = True
    elif project.status not in PRINTED_STATUSES and project.filament_deducted:
        for line in lines:
            spool = session.get(Filament, line.filament_id)
            if spool:
                spool.remaining_g = round(spool.remaining_g + line.deducted_g, 2)
                session.add(spool)
            line.deducted_g = 0
            session.add(line)
        project.filament_deducted = False


def _setting_number(session: Session, key: str, default: Optional[float]) -> Optional[float]:
    try:
        value = float(get_setting(session, key, "") or "")
    except ValueError:
        return default
    return value if value >= 0 else default


def _cost_breakdown(session: Session, lines: list, spools: dict, part_rows: list) -> dict:
    """What the project costs: filament (spools with a price), every part whether or not you
    already own it, and electricity when a price per kWh is set in Settings. Print time is
    derived from the grams, so it is as rough as the print estimate."""
    filament, unpriced, minutes = 0.0, 0.0, 0.0
    for line in lines:
        spool = spools.get(line.filament_id)
        if spool and spool.cost is not None and spool.spool_weight_g:
            filament += line.grams * spool.cost / spool.spool_weight_g
        else:
            unpriced += line.grams
        minutes += minutes_for_grams(line.grams, spool.material if spool else "PLA")
    priced = [p for p in part_rows if p["unit_cost"] is not None]
    parts = sum(p["quantity"] * p["unit_cost"] for p in priced)
    price = _setting_number(session, "cost_kwh_price", None)
    watts = _setting_number(session, "cost_printer_watts", 150.0) or 0.0
    hours = minutes / 60
    electricity = round(hours * watts / 1000 * price, 2) if price else None
    total = filament + parts + (electricity or 0)
    return {
        "filament": round(filament, 2), "filament_unpriced_g": round(unpriced, 1),
        "parts": round(parts, 2), "parts_unpriced": len(part_rows) - len(priced),
        "electricity": electricity, "print_hours": round(hours, 1), "printer_watts": watts,
        "total": round(total, 2),
    }


def _project_json(session: Session, project: Project) -> dict:
    parts = session.exec(
        select(ProjectPart).where(ProjectPart.project_id == project.id).order_by(ProjectPart.id)
    ).all()
    model_ids = session.exec(
        select(ProjectModelLink.model_id).where(ProjectModelLink.project_id == project.id)
    ).all()
    lines = session.exec(
        select(ProjectModelFilament).where(ProjectModelFilament.project_id == project.id).order_by(ProjectModelFilament.id)
    ).all()
    spools = {}
    if lines:
        spools = {f.id: f for f in session.exec(
            select(Filament).where(Filament.id.in_({l.filament_id for l in lines}))).all()}
    lines_by_model = {}
    for line in lines:
        lines_by_model.setdefault(line.model_id, []).append(_filament_line_json(line, spools.get(line.filament_id)))
    models = []
    if model_ids:
        models = [
            {"id": m.id, "filename": m.filename, "thumbnail_path": m.thumbnail_path,
             "extension": m.extension, "source_provider": m.source_provider, "source_id": m.source_id,
             "source_url": m.source_url, "source_title": m.source_title, "designer": m.designer, "license": m.license,
             "source_images": json.loads(m.source_images or "[]"),
             "filament": lines_by_model.get(m.id, []),
             "filament_grams": round(sum(l["grams"] for l in lines_by_model.get(m.id, [])), 2)}
            for m in session.exec(select(Model3D).where(Model3D.id.in_(model_ids))).all()
        ]
    # grams planned per spool, and whether that spool can cover it
    per_spool = {}
    for line in lines:
        spool = spools.get(line.filament_id)
        entry = per_spool.setdefault(line.filament_id, {
            "filament_id": line.filament_id, "filament_label": _filament_label(spool),
            "remaining_g": spool.remaining_g if spool else None, "grams": 0.0,
        })
        entry["grams"] = round(entry["grams"] + line.grams, 2)
    for entry in per_spool.values():
        # once deducted, remaining_g already reflects this project
        entry["short"] = (not project.filament_deducted and entry["remaining_g"] is not None
                          and entry["grams"] > entry["remaining_g"])
    part_rows = [_part_json(p) for p in parts]
    missing = [p for p in part_rows if p["quantity_needed"] > 0]
    return {
        **project.model_dump(),
        "parts": part_rows,
        "models": models,
        "filament_totals": list(per_spool.values()),
        "filament_grams": round(sum(l.grams for l in lines), 2),
        "parts_total": len(part_rows),
        "parts_missing": len(missing),
        "cost_needed": round(sum(p["cost_needed"] or 0 for p in missing), 2),
        "cost": _cost_breakdown(session, lines, spools, part_rows),
    }


def _get_project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Not found")
    return project


@router.get("")
def list_projects(only_needing_parts: bool = False, session: Session = Depends(get_session)):
    """All projects with their parts. only_needing_parts=true keeps just the
    projects that still have parts to acquire, each with only its missing parts."""
    projects = [_project_json(session, p) for p in session.exec(select(Project).order_by(Project.name)).all()]
    if only_needing_parts:
        projects = [p for p in projects if p["parts_missing"]]
        for p in projects:
            p["parts"] = [part for part in p["parts"] if part["quantity_needed"] > 0]
    return projects


def _shopping_items(session: Session, combine: bool) -> list:
    rows = session.exec(
        select(ProjectPart, Project)
        .join(Project, ProjectPart.project_id == Project.id)
        .where(Project.status != "done")
        .order_by(ProjectPart.category, ProjectPart.name)
    ).all()
    stock = {}
    for item in session.exec(select(InventoryItem)).all():
        key = item.name.strip().lower()
        stock[key] = stock.get(key, 0) + item.quantity

    items = []
    merged = {}
    for part, project in rows:
        needed = max(0, part.quantity - part.quantity_owned)
        if not needed:
            continue
        key = (part.name.strip().lower(), part.category)
        if combine and key in merged:
            entry = merged[key]
            entry["quantity_needed"] += needed
            entry["project_names"].append(project.name)
            if entry["unit_cost"] is None:
                entry["unit_cost"] = part.unit_cost
            entry["purchase_url"] = entry["purchase_url"] or part.purchase_url
            continue
        entry = {
            "part_id": part.id,
            "name": part.name,
            "category": part.category,
            "quantity_needed": needed,
            "unit_cost": part.unit_cost,
            "purchase_url": part.purchase_url,
            "project_id": project.id,
            "project_names": [project.name],
            "in_stock": stock.get(part.name.strip().lower()),
        }
        items.append(entry)
        if combine:
            merged[key] = entry
    for entry in items:
        entry["project_name"] = ", ".join(dict.fromkeys(entry["project_names"]))
        entry["cost_needed"] = (round(entry["quantity_needed"] * entry["unit_cost"], 2)
                                if entry["unit_cost"] is not None else None)
    return items


@router.get("/shopping-list")
def shopping_list(combine: bool = False, session: Session = Depends(get_session)):
    """Every part still needed across all unfinished projects, with the project
    that needs it, so one trip/order can cover everything. combine=true merges
    identical parts (same name and type) from different projects into one line.
    in_stock is the quantity of a same-named part in the inventory, as a hint."""
    items = _shopping_items(session, combine)
    return {"items": items, "total_cost": round(sum(i["cost_needed"] or 0 for i in items), 2),
            "low_filament": _low_filament(session)}


def _low_filament(session: Session) -> list:
    """Spools at or below the low-filament warning level (Settings), as things to reorder."""
    from app.stock_watch import low_filament_threshold
    threshold = low_filament_threshold(session)
    if threshold <= 0:
        return []
    rows = session.exec(select(Filament).where(Filament.remaining_g <= threshold).order_by(Filament.material, Filament.color)).all()
    return [{"id": f.id, "label": " ".join(x for x in (f.material, f.brand, f.color) if x), "remaining_g": f.remaining_g,
             "cost": f.cost, "purchase_url": f.purchase_url} for f in rows]


def _spreadsheet_safe(value) -> str:
    """Stop a cell that starts with = + - @ from being run as a formula when the
    CSV is opened in Excel/Sheets (a part name or note is user-typed text)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/shopping-list/export")
def export_shopping_list(format: str = "csv", combine: bool = False, session: Session = Depends(get_session)):
    if format not in ("csv", "txt"):
        raise HTTPException(400, "format must be csv or txt")
    items = _shopping_items(session, combine)
    total = round(sum(i["cost_needed"] or 0 for i in items), 2)
    stamp = datetime.utcnow().strftime("%Y-%m-%d")

    if format == "csv":
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Part", "Type", "Quantity", "Unit cost", "Line cost", "In stock", "Projects", "Link"])
        for i in items:
            writer.writerow([
                _spreadsheet_safe(i["name"]), i["category"], i["quantity_needed"],
                "" if i["unit_cost"] is None else f"{i['unit_cost']:.2f}",
                "" if i["cost_needed"] is None else f"{i['cost_needed']:.2f}",
                "" if i["in_stock"] is None else i["in_stock"],
                _spreadsheet_safe(i["project_name"]), _spreadsheet_safe(i["purchase_url"]),
            ])
        writer.writerow([])
        writer.writerow(["Estimated total", "", "", "", f"{total:.2f}"])
        low = _low_filament(session)
        if low:
            writer.writerow([])
            writer.writerow(["Filament running low", "", "Left (g)", "Spool price", "", "", "", "Link"])
            for f in low:
                writer.writerow([_spreadsheet_safe(f["label"]), "filament", f"{f['remaining_g']:g}",
                                 "" if f["cost"] is None else f"{f['cost']:.2f}", "", "", "", _spreadsheet_safe(f["purchase_url"])])
        body, media = out.getvalue(), "text/csv"
    else:
        lines = [f"Shopping list - {stamp}", f"{len(items)} item(s), estimated ${total:.2f}", ""]
        for category in PART_CATEGORIES:
            group = [i for i in items if i["category"] == category]
            if not group:
                continue
            lines.append(category.upper())
            for i in group:
                cost = f" - ${i['cost_needed']:.2f}" if i["cost_needed"] is not None else ""
                stock = f" (have {i['in_stock']} in stock)" if i["in_stock"] else ""
                lines.append(f"[ ] {i['quantity_needed']} x {i['name']}{cost}{stock}  [{i['project_name']}]")
                if i["purchase_url"]:
                    lines.append(f"    {i['purchase_url']}")
            lines.append("")
        low = _low_filament(session)
        if low:
            lines.append("FILAMENT RUNNING LOW")
            for f in low:
                lines.append(f"[ ] {f['label']} ({f['remaining_g']:g} g left)" + (f" - ${f['cost']:.2f}" if f["cost"] is not None else ""))
                if f["purchase_url"]:
                    lines.append(f"    {f['purchase_url']}")
            lines.append("")
        body, media = "\n".join(lines).rstrip() + "\n", "text/plain"

    filename = f"shopping-list-{stamp}.{format}"
    return Response(body, media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("")
def create_project(payload: dict, session: Session = Depends(get_session)):
    project = Project(
        name=_clean_text(payload.get("name"), "name", required=True),
        description=_clean_text(payload.get("description"), "description"),
        notes=_clean_text(payload.get("notes"), "notes"),
        status=_clean_choice(payload.get("status", "planning"), "status", PROJECT_STATUSES),
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return _project_json(session, project)


@router.get("/{project_id}")
def get_project(project_id: int, session: Session = Depends(get_session)):
    return _project_json(session, _get_project(session, project_id))


@router.get("/{project_id}/export.pdf")
def export_project_pdf(project_id: int, session: Session = Depends(get_session)):
    """The whole project as a PDF: description, notes, models, filament and parts list."""
    from app.project_pdf import build_project_pdf
    project = _project_json(session, _get_project(session, project_id))
    slug = re.sub(r"[^a-z0-9]+", "-", project["name"].lower()).strip("-") or "project"
    return Response(build_project_pdf(project), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="project-{slug[:60]}.pdf"'})


@router.patch("/{project_id}")
def update_project(project_id: int, payload: dict, session: Session = Depends(get_session)):
    project = _get_project(session, project_id)
    if "name" in payload:
        project.name = _clean_text(payload["name"], "name", required=True)
    if "description" in payload:
        project.description = _clean_text(payload["description"], "description")
    if "notes" in payload:
        project.notes = _clean_text(payload["notes"], "notes")
    if "status" in payload:
        project.status = _clean_choice(payload["status"], "status", PROJECT_STATUSES)
        _apply_filament_status(session, project)
    session.add(project)
    session.commit()
    session.refresh(project)
    return _project_json(session, project)


@router.delete("/{project_id}")
def delete_project(project_id: int, session: Session = Depends(get_session)):
    project = _get_project(session, project_id)
    for part in session.exec(select(ProjectPart).where(ProjectPart.project_id == project_id)).all():
        session.delete(part)
    for link in session.exec(select(ProjectModelLink).where(ProjectModelLink.project_id == project_id)).all():
        session.delete(link)
    for line in session.exec(select(ProjectModelFilament).where(ProjectModelFilament.project_id == project_id)).all():
        session.delete(line)
    session.delete(project)
    session.commit()
    return {"status": "deleted"}


# --- Parts (bill of materials) ---

def _build_part(project_id: int, payload: dict) -> ProjectPart:
    if not isinstance(payload, dict):
        raise HTTPException(400, "each part must be an object")
    return ProjectPart(
        project_id=project_id,
        name=_clean_text(payload.get("name"), "name", required=True),
        category=_clean_choice(payload.get("category", "electronics"), "category", PART_CATEGORIES),
        quantity=_clean_int(payload.get("quantity", 1), "quantity", 1),
        quantity_owned=_clean_int(payload.get("quantity_owned", 0), "quantity_owned", 0),
        unit_cost=_clean_cost(payload.get("unit_cost")),
        purchase_url=_clean_text(payload.get("purchase_url"), "purchase_url"),
        notes=_clean_text(payload.get("notes"), "notes"),
    )


@router.post("/{project_id}/parts")
def add_part(project_id: int, payload: dict, session: Session = Depends(get_session)):
    _get_project(session, project_id)
    part = _build_part(project_id, payload)
    session.add(part)
    session.commit()
    session.refresh(part)
    return _part_json(part)


MAX_BULK_PARTS = 200


@router.post("/{project_id}/parts/bulk")
def add_parts_bulk(project_id: int, payload: dict, session: Session = Depends(get_session)):
    """Add several parts at once (e.g. a listing's parts list). All are checked
    first, so one bad row adds nothing rather than half the list."""
    _get_project(session, project_id)
    rows = payload.get("parts")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(400, "parts must be a non-empty list")
    if len(rows) > MAX_BULK_PARTS:
        raise HTTPException(400, f"at most {MAX_BULK_PARTS} parts at a time")
    parts = []
    for index, row in enumerate(rows, start=1):
        try:
            parts.append(_build_part(project_id, row))
        except HTTPException as e:
            raise HTTPException(400, f"part {index}: {e.detail}")
    session.add_all(parts)
    session.commit()
    for part in parts:
        session.refresh(part)
    return {"added": len(parts), "parts": [_part_json(p) for p in parts]}


@router.patch("/{project_id}/parts/{part_id}")
def update_part(project_id: int, part_id: int, payload: dict, session: Session = Depends(get_session)):
    part = session.get(ProjectPart, part_id)
    if not part or part.project_id != project_id:
        raise HTTPException(404, "Not found")
    if "name" in payload:
        part.name = _clean_text(payload["name"], "name", required=True)
    if "category" in payload:
        part.category = _clean_choice(payload["category"], "category", PART_CATEGORIES)
    if "quantity" in payload:
        part.quantity = _clean_int(payload["quantity"], "quantity", 1)
    if "quantity_owned" in payload:
        part.quantity_owned = _clean_int(payload["quantity_owned"], "quantity_owned", 0)
    if "unit_cost" in payload:
        part.unit_cost = _clean_cost(payload["unit_cost"])
    if "purchase_url" in payload:
        part.purchase_url = _clean_text(payload["purchase_url"], "purchase_url")
    if "notes" in payload:
        part.notes = _clean_text(payload["notes"], "notes")
    session.add(part)
    session.commit()
    session.refresh(part)
    return _part_json(part)


@router.delete("/{project_id}/parts/{part_id}")
def delete_part(project_id: int, part_id: int, session: Session = Depends(get_session)):
    part = session.get(ProjectPart, part_id)
    if not part or part.project_id != project_id:
        raise HTTPException(404, "Not found")
    session.delete(part)
    session.commit()
    return {"status": "deleted"}


# --- Linked 3D models ---

@router.post("/{project_id}/models/{model_id}")
def link_model(project_id: int, model_id: int, session: Session = Depends(get_session)):
    _get_project(session, project_id)
    if not session.get(Model3D, model_id):
        raise HTTPException(404, "Not found")
    if not session.get(ProjectModelLink, (project_id, model_id)):
        session.add(ProjectModelLink(project_id=project_id, model_id=model_id))
        session.commit()
    return _project_json(session, _get_project(session, project_id))


@router.delete("/{project_id}/models/{model_id}")
def unlink_model(project_id: int, model_id: int, session: Session = Depends(get_session)):
    link = session.get(ProjectModelLink, (project_id, model_id))
    if not link:
        raise HTTPException(404, "Not found")
    project = _get_project(session, project_id)
    _reject_if_deducted(project)
    for line in session.exec(select(ProjectModelFilament).where(
            ProjectModelFilament.project_id == project_id, ProjectModelFilament.model_id == model_id)).all():
        session.delete(line)
    session.delete(link)
    session.commit()
    return _project_json(session, project)


# --- Filament per model ---

@router.post("/{project_id}/models/{model_id}/filament")
def add_model_filament(project_id: int, model_id: int, payload: dict, session: Session = Depends(get_session)):
    project = _get_project(session, project_id)
    if not session.get(ProjectModelLink, (project_id, model_id)):
        raise HTTPException(404, "Model is not in this project")
    _reject_if_deducted(project)
    try:
        filament_id = int(payload.get("filament_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "filament_id is required")
    if not session.get(Filament, filament_id):
        raise HTTPException(404, "Filament not found")
    session.add(ProjectModelFilament(
        project_id=project_id, model_id=model_id, filament_id=filament_id,
        grams=_clean_grams(payload.get("grams", 0)),
    ))
    session.commit()
    return _project_json(session, project)


@router.get("/{project_id}/models/{model_id}/filament-suggestions")
def filament_suggestions(project_id: int, model_id: int, session: Session = Depends(get_session)):
    """The filament the model's site listing recommends (MakerWorld), each matched
    to one of your spools where there is one that fits."""
    _get_project(session, project_id)
    if not session.get(ProjectModelLink, (project_id, model_id)):
        raise HTTPException(404, "Model is not in this project")
    model = session.get(Model3D, model_id)
    try:
        suggested = json.loads(model.source_filaments or "[]")
    except ValueError:
        suggested = []
    spools = session.exec(select(Filament)).all()
    used = {line.filament_id for line in session.exec(select(ProjectModelFilament).where(
        ProjectModelFilament.project_id == project_id, ProjectModelFilament.model_id == model_id)).all()}
    out = []
    for s in suggested:
        spool = match_spool(s, spools)
        out.append({
            **s,
            "spool_id": spool.id if spool else None,
            "spool_label": spool_label(spool) if spool else None,
            "remaining_g": spool.remaining_g if spool else None,
            "already_added": bool(spool and spool.id in used),
        })
    return out


@router.patch("/{project_id}/filament/{line_id}")
def update_model_filament(project_id: int, line_id: int, payload: dict, session: Session = Depends(get_session)):
    project = _get_project(session, project_id)
    line = session.get(ProjectModelFilament, line_id)
    if not line or line.project_id != project_id:
        raise HTTPException(404, "Not found")
    _reject_if_deducted(project)
    if "grams" in payload:
        line.grams = _clean_grams(payload["grams"])
    if "filament_id" in payload:
        try:
            filament_id = int(payload["filament_id"])
        except (TypeError, ValueError):
            raise HTTPException(400, "filament_id must be a number")
        if not session.get(Filament, filament_id):
            raise HTTPException(404, "Filament not found")
        line.filament_id = filament_id
    session.add(line)
    session.commit()
    return _project_json(session, project)


@router.delete("/{project_id}/filament/{line_id}")
def delete_model_filament(project_id: int, line_id: int, session: Session = Depends(get_session)):
    project = _get_project(session, project_id)
    line = session.get(ProjectModelFilament, line_id)
    if not line or line.project_id != project_id:
        raise HTTPException(404, "Not found")
    _reject_if_deducted(project)
    session.delete(line)
    session.commit()
    return _project_json(session, project)
