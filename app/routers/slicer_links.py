"""Open a model in a slicer on your computer: PrusaSlicer, OrcaSlicer and Bambu Studio can open a model from an address."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from app import signed_links
from app.config import LIBRARY_PATH
from app.db import get_session
from app.models import Model3D

router = APIRouter(prefix="/api/slicer-link", tags=["slicer-link"])
public_router = APIRouter(prefix="/dl", tags=["slicer-download"], include_in_schema=False)

OPENABLE = {".stl", ".3mf", ".obj", ".step", ".stp"}
SCHEMES = {"prusaslicer": "prusaslicer://open?file=", "orcaslicer": "orcaslicer://open?file=", "bambustudio": "bambustudio://open?file="}


@router.get("/{model_id}")
def slicer_link(model_id: int, session: Session = Depends(get_session)):
    """A link that works for 15 minutes, for the slicer to fetch this model's file. The page puts its own address in front of it."""
    model = session.get(Model3D, model_id)
    if not model:
        raise HTTPException(404, "Model not found")
    if model.extension.lower() not in OPENABLE:
        raise HTTPException(400, "Slicers open .stl, .3mf, .obj and .step files")
    expires, signature = signed_links.make(model_id)
    name = "".join(c if c.isalnum() or c in "._-" else "_" for c in model.filename)[:100] or "model"
    return {"path": f"/dl/{model_id}/{expires}/{signature}/{name}", "expires_in": signed_links.TTL_SECONDS, "schemes": SCHEMES}


@public_router.get("/{model_id}/{expires}/{signature}/{name}")
def download(model_id: int, expires: str, signature: str, name: str, session: Session = Depends(get_session)):
    if not signed_links.valid(model_id, expires, signature):
        raise HTTPException(404, "This link has expired")
    model = session.get(Model3D, model_id)
    path = (LIBRARY_PATH / model.path) if model else None
    if not model or not path.is_file():
        raise HTTPException(404, "This link has expired")
    return FileResponse(path, filename=model.filename, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
