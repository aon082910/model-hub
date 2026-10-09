"""The phone's share menu: with Model Hub installed as an app, "Share" on an STL, 3MF, OBJ, STEP or ZIP offers Model Hub, which files it in the library."""
from typing import List

from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app.db import get_session
from app.config import ARCHIVE_EXTENSIONS
from app.scanner import import_uploaded_archive, import_uploaded_file

router = APIRouter(tags=["share-target"], include_in_schema=False)
MAX_FILES = 10


def _page(message: str, status: int) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Model Hub</title>"
                        f"<body style='font-family:system-ui;background:#14161a;color:#e7e9ee;padding:24px'><p>{message}</p><p><a style='color:#6aa5ff' href='/'>Open Model Hub</a></p>",
                        status_code=status, headers={"Cache-Control": "no-store"})


@router.post("/share-target")
async def share_target(file: List[UploadFile] = File(...), session: Session = Depends(get_session)):
    """Files shared from the phone. Accepted: model files and ZIPs of them. Opens the library afterwards."""
    from html import escape
    problems, added = [], 0
    for upload in file[:MAX_FILES]:
        content = await upload.read()
        name = upload.filename or "shared"
        try:
            if any(name.lower().endswith(ext) for ext in ARCHIVE_EXTENSIONS):
                added += len((await run_in_threadpool(import_uploaded_archive, session, name, content))["models"])
            else:
                await run_in_threadpool(import_uploaded_file, session, name, content)
                added += 1
        except ValueError as e:
            problems.append(f"{escape(name[:80])}: {escape(str(e))}")
    if added == 0 and problems:
        return _page("Nothing was added.<br>" + "<br>".join(problems), 400)
    return RedirectResponse("/#/library", status_code=303)
