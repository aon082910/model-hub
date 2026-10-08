from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from app import source_match_jobs as jobs
from app.db import get_session

router = APIRouter(prefix="/api/source-match", tags=["source-match"])


@router.get("/status")
def match_status(session: Session = Depends(get_session)):
    return {"job": jobs.job_status(), "summary": jobs.summary(session)}


@router.post("/start")
def start_matching(payload: dict = None):
    """Look up matches for every model not linked yet. recheck_none=true also
    searches again for models an earlier run found nothing for. auto_link_min
    (0.9 to 1.0) additionally links, without asking, any model whose best match
    scores at least that and is clearly ahead of the runner-up."""
    payload = payload or {}
    auto_link_min = payload.get("auto_link_min")
    if auto_link_min is not None:
        try:
            auto_link_min = float(auto_link_min)
        except (TypeError, ValueError):
            raise HTTPException(400, "auto_link_min must be a number")
        if not jobs.AUTO_LINK_FLOOR <= auto_link_min <= 1.0:
            raise HTTPException(400, f"auto_link_min must be between {jobs.AUTO_LINK_FLOOR} and 1.0")
    try:
        return jobs.start_job(recheck_none=bool(payload.get("recheck_none")), auto_link_min=auto_link_min)
    except jobs.JobBusy as e:
        raise HTTPException(409, str(e))


@router.post("/stop")
def stop_matching():
    return jobs.stop_job()


@router.get("/queue")
def review_queue(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    session: Session = Depends(get_session),
):
    return jobs.review_queue(session, offset, limit, min_score)


@router.post("/models/{model_id}/skip")
def skip(model_id: int, session: Session = Depends(get_session)):
    if not jobs.skip_model(session, model_id):
        raise HTTPException(404, "Model not found")
    return {"status": "skipped"}


@router.get("/auto-linked")
def auto_linked(session: Session = Depends(get_session)):
    return jobs.auto_linked_models(session)


@router.post("/models/{model_id}/confirm")
def confirm(model_id: int, session: Session = Depends(get_session)):
    if not jobs.confirm_auto_link(session, model_id):
        raise HTTPException(404, "That model was not auto-linked")
    return {"status": "confirmed"}
