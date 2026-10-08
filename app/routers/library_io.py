from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from sqlmodel import Session

from app import activity, library_io
from app.db import get_session

router = APIRouter(prefix="/api/library-io", tags=["library-io"])


def _stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M")


@router.get("/export.json")
def export_json(session: Session = Depends(get_session)):
    """Everything Model Hub knows about the models (not the files)."""
    return Response(library_io.export_json(session), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="model-hub-library-{_stamp()}.json"'})


@router.get("/export.csv")
def export_csv(session: Session = Depends(get_session)):
    return Response(library_io.export_csv(session), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="model-hub-library-{_stamp()}.csv"'})


@router.post("/import")
def import_metadata(request: Request, file: UploadFile = File(...), overwrite: bool = Form(False), dry_run: bool = Form(False),
                    session: Session = Depends(get_session)):
    """Add the information in an export (or a spreadsheet with path/filename, tags, collections, designer, license, notes...)
    to the models it matches. Existing designer/license/notes are kept unless overwrite is true."""
    data = file.file.read(library_io.MAX_IMPORT_BYTES + 1)
    try:
        rows = library_io.parse_upload(data, file.filename or "")
    except library_io.ImportError_ as e:
        raise HTTPException(400, str(e))
    result = library_io.apply_import(session, rows, overwrite=overwrite, dry_run=dry_run)
    if not dry_run and result["models_changed"]:
        activity.record(session, activity.actor_of(request), "import",
                        f"Imported library information: {result['models_changed']} model(s) changed ({result['tags_added']} tags, "
                        f"{result['collections_added']} collection links, {result['fields_set']} fields)")
    return result
