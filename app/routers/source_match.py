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
    searches again for models an earlier run found nothing for."""
    try:
        return jobs.start_job(recheck_none=bool((payload or {}).get("recheck_none")))
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
