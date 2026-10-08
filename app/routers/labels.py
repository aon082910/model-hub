"""QR codes for labels (spool and supply labels open the right page when scanned with a phone camera)."""
import io

import segno
from fastapi import APIRouter, HTTPException, Query, Response

router = APIRouter(prefix="/api/qr", tags=["labels"])

MAX_TEXT = 300


@router.get("")
def qr_code(text: str = Query(..., min_length=1)):
    """A QR code as an SVG for the given text (a link to the item)."""
    if len(text) > MAX_TEXT:
        raise HTTPException(400, f"At most {MAX_TEXT} characters")
    buffer = io.BytesIO()
    segno.make(text, error="m").save(buffer, kind="svg", scale=4, border=2, xmldecl=False, svgns=True, nl=False)
    return Response(buffer.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=3600"})
