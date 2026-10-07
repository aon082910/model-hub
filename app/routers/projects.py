from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from app.db import get_session
from app.models import Filament, Model3D, Project, ProjectModelFilament, ProjectModelLink, ProjectPart

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
             "extension": m.extension, "filament": lines_by_model.get(m.id, []),
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


@router.get("/shopping-list")
def shopping_list(session: Session = Depends(get_session)):
    """Every part still needed across all unfinished projects, with the project
    that needs it, so one trip/order can cover everything."""
    rows = session.exec(
        select(ProjectPart, Project)
        .join(Project, ProjectPart.project_id == Project.id)
        .where(Project.status != "done")
        .order_by(ProjectPart.category, ProjectPart.name)
    ).all()
    items = []
    for part, project in rows:
        needed = max(0, part.quantity - part.quantity_owned)
        if not needed:
            continue
        items.append({
            "part_id": part.id,
            "name": part.name,
            "category": part.category,
            "quantity_needed": needed,
            "unit_cost": part.unit_cost,
            "cost_needed": round(needed * part.unit_cost, 2) if part.unit_cost is not None else None,
            "purchase_url": part.purchase_url,
            "project_id": project.id,
            "project_name": project.name,
        })
    return {"items": items, "total_cost": round(sum(i["cost_needed"] or 0 for i in items), 2)}


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

@router.post("/{project_id}/parts")
def add_part(project_id: int, payload: dict, session: Session = Depends(get_session)):
    _get_project(session, project_id)
    part = ProjectPart(
        project_id=project_id,
        name=_clean_text(payload.get("name"), "name", required=True),
        category=_clean_choice(payload.get("category", "electronics"), "category", PART_CATEGORIES),
        quantity=_clean_int(payload.get("quantity", 1), "quantity", 1),
        quantity_owned=_clean_int(payload.get("quantity_owned", 0), "quantity_owned", 0),
        unit_cost=_clean_cost(payload.get("unit_cost")),
        purchase_url=_clean_text(payload.get("purchase_url"), "purchase_url"),
        notes=_clean_text(payload.get("notes"), "notes"),
    )
    session.add(part)
    session.commit()
    session.refresh(part)
    return _part_json(part)


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
