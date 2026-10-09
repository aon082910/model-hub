"""Send a sliced Bambu job (.gcode.3mf) to a Bambu printer in LAN mode. Kept apart from routers/printers.py, which still handles
plain G-code for Moonraker and OctoPrint. Admin only, like the rest of /api/printers."""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session

from app import bambu_send, print_files, printers as printing
from app.db import get_session
from app.models import PrinterJob, PrintFile
from app.routers.printers import _get

router = APIRouter(prefix="/api/printers", tags=["printers"])


@router.post("/{printer_id}/send-3mf")
def send_3mf(printer_id: int, file: Optional[UploadFile] = File(None), print_file_id: Optional[int] = Form(None),
             start: bool = Form(False), plate: Optional[int] = Form(None), use_ams: bool = Form(True),
             ams_mapping: str = Form(""), bed_type: str = Form("textured_plate"),
             session: Session = Depends(get_session)):
    """Upload a sliced .gcode.3mf (uploaded now, or one kept with a model) to a Bambu printer's card.
    start=true also starts it; ams_mapping is the slot for each filament, like "0,1"."""
    printer = _get(session, printer_id)
    if printer.kind != "bambu":
        raise HTTPException(400, "This is for Bambu printers; the others take G-code through /send")
    if (file is None) == (print_file_id is None):
        raise HTTPException(400, "Give either a sliced .gcode.3mf file or the id of a kept sliced file")
    try:
        mapping = bambu_send.parse_mapping(ams_mapping)
        with printing.temp_dir() as tmp:
            workdir = Path(tmp)
            model_id = None
            if file is not None:
                if not (file.filename or "").lower().endswith(".3mf"):
                    raise HTTPException(400, "That is not a .3mf file")
                name = bambu_send.job_name(file.filename)
                path = workdir / name
                written = 0
                with open(path, "wb") as out:
                    while chunk := file.file.read(1024 * 1024):
                        written += len(chunk)
                        if written > printing.MAX_UPLOAD_BYTES:
                            raise printing.PrinterError("That file is larger than 1 GB")
                        out.write(chunk)
            else:
                kept = session.get(PrintFile, print_file_id)
                if not kept:
                    raise HTTPException(404, "That sliced file was not found")
                if kept.kind != "3mf":
                    raise HTTPException(400, "A Bambu printer takes a sliced .gcode.3mf; this kept file is plain G-code")
                path = print_files.stored_path(kept.stored_name)
                if not path.is_file():
                    raise HTTPException(404, "The file is missing from this server")
                model_id = kept.model_id
                name = bambu_send.job_name(kept.filename)
            result = bambu_send.send_job(printer.url, printer.serial or "", printer.api_key or "", path, name, start,
                                         plate=plate, use_ams=use_ams, ams_mapping=mapping, bed_type=bed_type)
            session.add(PrinterJob(printer_id=printer.id, filename=result["filename"], model_id=model_id,
                                   print_file_id=print_file_id, started=bool(result["started"])))
            session.commit()
            return result
    except printing.PrinterError as e:
        raise HTTPException(502, str(e))
