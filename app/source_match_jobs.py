"""Background job: look up Printables / MakerWorld matches for every library
model that is not linked to a listing yet, and keep the best few as candidates
to review. By default nothing is linked automatically -- a match found by file name can be
wrong, so a person accepts or rejects each one (see the Matches tab). An opt-in
"auto-link" threshold links only clear winners (a very high score, and well
clear of the runner-up); those are marked so they can be double-checked.

The job is gentle on the sites (one pair of searches, then a pause), stops by
itself if a site starts rate limiting or keeps failing, and can be resumed any
time: models it already looked at are not looked at again unless asked to.
"""
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app import sources
from app.config import MODEL_EXTENSIONS
from app.db import engine
from app.models import Model3D, SourceCandidate, SourceMatchState

logger = logging.getLogger("modelhub.sourcematch")

MIN_SCORE = 0.45                 # candidates scoring below this are not worth showing
MAX_CANDIDATES = 3
PAUSE_SECONDS = 1.0              # between models
BACKOFF_SECONDS = 5.0            # after a model where both sites failed
MAX_CONSECUTIVE_FAILURES = 5
AUTO_LINK_MARGIN = 0.1           # an auto-link also needs to beat the runner-up by this much
AUTO_LINK_FLOOR = 0.9            # never auto-link below this, whatever is asked


class JobBusy(Exception):
    pass


_lock = threading.Lock()
_stop = threading.Event()
_state = {
    "running": False, "started_at": None, "finished_at": None,
    "total": 0, "checked": 0, "with_candidates": 0, "no_match": 0, "auto_linked": 0, "errors": 0, "message": "",
    "auto_link_min": None,
}


def _set(**values):
    with _lock:
        _state.update(values)


def _bump(key: str):
    with _lock:
        _state[key] += 1


def job_status() -> dict:
    with _lock:
        return dict(_state)


def clear_match_data(session: Session, model_id: int) -> None:
    """Forget a model's candidates and review state (it was linked or removed).
    Does not commit; the caller does."""
    for candidate in session.exec(select(SourceCandidate).where(SourceCandidate.model_id == model_id)).all():
        session.delete(candidate)
    state = session.get(SourceMatchState, model_id)
    if state:
        session.delete(state)


def summary(session: Session) -> dict:
    """Live counts for the Matches tab."""
    is_model = Model3D.extension.in_(MODEL_EXTENSIONS)
    total = session.exec(select(func.count()).select_from(Model3D).where(is_model)).one()
    linked = session.exec(
        select(func.count()).select_from(Model3D).where(is_model, Model3D.source_provider.is_not(None))).one()
    by_status = dict(session.exec(
        select(SourceMatchState.status, func.count())
        .join(Model3D, Model3D.id == SourceMatchState.model_id)
        .where(is_model, Model3D.source_provider.is_(None))
        .group_by(SourceMatchState.status)
    ).all())
    waiting, none, skipped = by_status.get("candidates", 0), by_status.get("none", 0), by_status.get("skipped", 0)
    unlinked = total - linked
    return {
        "total": total, "linked": linked, "unlinked": unlinked,
        "waiting_review": waiting, "no_match": none, "skipped": skipped,
        "unchecked": max(0, unlinked - waiting - none - skipped),
    }


def _models_to_check(recheck_none: bool, only_ids: Optional[list] = None) -> list:
    with Session(engine) as session:
        if recheck_none:
            stale = select(SourceMatchState).where(SourceMatchState.status == "none")
            if only_ids is not None:
                stale = stale.where(SourceMatchState.model_id.in_(only_ids))
            for state in session.exec(stale).all():
                session.delete(state)
            session.commit()
        done = select(SourceMatchState.model_id)
        wanted = select(Model3D.id).where(
            Model3D.extension.in_(MODEL_EXTENSIONS), Model3D.source_provider.is_(None), Model3D.id.not_in(done))
        if only_ids is not None:
            wanted = wanted.where(Model3D.id.in_(only_ids))
        return list(session.exec(wanted.order_by(Model3D.id)).all())


def clear_winner(ranked: list, minimum: float) -> bool:
    """True when the best candidate is good enough, and far enough ahead of the
    runner-up, to be linked without asking."""
    if not ranked or minimum is None:
        return False
    best = ranked[0]["score"]
    runner_up = ranked[1]["score"] if len(ranked) > 1 else 0.0
    return best >= max(minimum, AUTO_LINK_FLOOR) and best - runner_up >= AUTO_LINK_MARGIN


def _check_one(model_id: int, auto_link_min: Optional[float] = None) -> str:
    """Look one model up. Returns 'candidates', 'none', 'linked' (auto-linked),
    'error' or 'limited' (a site asked us to slow down)."""
    with Session(engine) as session:
        model = session.get(Model3D, model_id)
        if not model or model.source_provider:
            return "none"
        query = sources.suggest_query(model.filename)
        if not query:   # a name like 'a1b2c3d4.stl' says nothing worth searching for
            session.add(SourceMatchState(model_id=model_id, status="none", query=""))
            session.commit()
            return "none"
        credentials = sources.load_credentials(session)
        providers = sources.matching_providers(credentials)
        found = sources.search(query, providers, limit=6, credentials=credentials)
        errors = found["errors"]
        if errors and len(errors) >= len(providers):
            return "limited" if any("rate limiting" in m for m in errors.values()) else "error"

        ranked = [r for r in sources.rank(found["results"], query) if r["score"] >= MIN_SCORE][:MAX_CANDIDATES]
        if not ranked and errors:
            return "error"     # one site was down and the other had nothing: look again next time
        if clear_winner(ranked, auto_link_min):
            from app.source_linking import link_model_to_listing   # lazy: that module imports this one
            try:
                link_model_to_listing(session, model, ranked[0]["provider"], ranked[0]["source_id"],
                                      images=True, fill_details=True, add_tags=False, linked_by="auto")
                return "linked"
            except sources.SourceError as e:
                logger.info("Auto-link for %s failed (%s); keeping it for review", model.filename, e)
                session.rollback()
        for candidate in session.exec(select(SourceCandidate).where(SourceCandidate.model_id == model_id)).all():
            session.delete(candidate)
        status = "candidates" if ranked else "none"
        state = session.get(SourceMatchState, model_id) or SourceMatchState(model_id=model_id)
        state.status, state.query, state.checked_at = status, query, datetime.utcnow()
        session.add(state)
        for r in ranked:
            session.add(SourceCandidate(
                model_id=model_id, provider=r["provider"], source_id=r["source_id"], title=r["title"][:300],
                designer=r.get("designer"), license=r.get("license"), thumbnail=r.get("thumbnail"),
                url=r.get("url"), score=r["score"]))
        session.commit()
        return status


def run_job(recheck_none: bool = False, only_ids: Optional[list] = None, auto_link_min: Optional[float] = None) -> None:
    """The job body (runs in its own thread; callable directly in tests).
    only_ids limits it to those models; normally it covers the whole library."""
    try:
        ids = _models_to_check(recheck_none, only_ids)
        _set(total=len(ids), message="Searching..." if ids else "Nothing left to check.")
        failures = 0
        for model_id in ids:
            if _stop.is_set():
                _set(message="Stopped. Start again to continue where it left off.")
                return
            try:
                outcome = _check_one(model_id, auto_link_min)
            except sources.SourceError as e:
                outcome = "error"
                logger.info("Match lookup failed: %s", e)
            except Exception:
                logger.exception("Match lookup crashed for model %s", model_id)
                outcome = "error"

            if outcome in ("error", "limited"):
                failures += 1
                _bump("errors")
                if outcome == "limited" or failures >= MAX_CONSECUTIVE_FAILURES:
                    _set(message=("A site is rate limiting requests" if outcome == "limited"
                                  else "The sites keep failing") + ". Stopped; try again in a while.")
                    return
                _stop.wait(BACKOFF_SECONDS)
                continue
            failures = 0
            _bump("checked")
            _bump({"candidates": "with_candidates", "linked": "auto_linked"}.get(outcome, "no_match"))
            _stop.wait(PAUSE_SECONDS)
        _set(message="Finished.")
    except Exception:
        logger.exception("Match job failed")
        _set(message="The job hit an unexpected error; see the container log.")
    finally:
        _set(running=False, finished_at=datetime.utcnow().isoformat())


def start_job(recheck_none: bool = False, auto_link_min: Optional[float] = None) -> dict:
    with _lock:
        if _state["running"]:
            raise JobBusy("A matching job is already running")
        _state.update(running=True, started_at=datetime.utcnow().isoformat(), finished_at=None,
                      total=0, checked=0, with_candidates=0, no_match=0, auto_linked=0, errors=0,
                      message="Starting...", auto_link_min=auto_link_min)
    _stop.clear()
    threading.Thread(target=run_job, args=(recheck_none, None, auto_link_min), name="source-match", daemon=True).start()
    return job_status()


def stop_job() -> dict:
    _stop.set()
    return job_status()


def best_scores_subquery():
    return (select(SourceCandidate.model_id, func.max(SourceCandidate.score).label("best"))
            .group_by(SourceCandidate.model_id).subquery())


def review_queue(session: Session, offset: int, limit: int, min_score: float) -> dict:
    """Models waiting for a decision, best match first, each with its candidates."""
    best = best_scores_subquery()
    base = (
        select(Model3D, best.c.best)
        .join(SourceMatchState, SourceMatchState.model_id == Model3D.id)
        .join(best, best.c.model_id == Model3D.id)
        .where(SourceMatchState.status == "candidates", Model3D.source_provider.is_(None), best.c.best >= min_score)
    )
    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    rows = session.exec(base.order_by(best.c.best.desc(), Model3D.id).offset(offset).limit(limit)).all()
    ids = [m.id for m, _ in rows]
    by_model = {}
    if ids:
        for c in session.exec(select(SourceCandidate).where(SourceCandidate.model_id.in_(ids))
                              .order_by(SourceCandidate.score.desc())).all():
            by_model.setdefault(c.model_id, []).append(c.model_dump(exclude={"model_id"}))
    return {
        "total": total,
        "items": [{
            "model": {"id": m.id, "filename": m.filename, "extension": m.extension, "thumbnail_path": m.thumbnail_path},
            "best_score": best_score,
            "candidates": by_model.get(m.id, []),
        } for m, best_score in rows],
    }


def skip_model(session: Session, model_id: int) -> Optional[SourceMatchState]:
    """'None of these': keep the model out of the queue (and out of future runs)."""
    if not session.get(Model3D, model_id):
        return None
    for candidate in session.exec(select(SourceCandidate).where(SourceCandidate.model_id == model_id)).all():
        session.delete(candidate)
    state = session.get(SourceMatchState, model_id) or SourceMatchState(model_id=model_id)
    state.status, state.checked_at = "skipped", datetime.utcnow()
    session.add(state)
    session.commit()
    return state


def auto_linked_models(session: Session, limit: int = 50) -> list:
    """Models the job linked on its own, newest first, for a second look."""
    rows = session.exec(
        select(Model3D).where(Model3D.source_linked_by == "auto").order_by(Model3D.updated_at.desc()).limit(limit)
    ).all()
    return [{
        "id": m.id, "filename": m.filename, "extension": m.extension, "thumbnail_path": m.thumbnail_path,
        "source_provider": m.source_provider, "source_title": m.source_title, "source_url": m.source_url,
        "designer": m.designer,
    } for m in rows]


def confirm_auto_link(session: Session, model_id: int) -> bool:
    """'Looks right': the link stays, and it stops being listed for review."""
    model = session.get(Model3D, model_id)
    if not model or model.source_linked_by != "auto":
        return False
    model.source_linked_by = "manual"
    session.add(model)
    session.commit()
    return True


def _linked_filter(provider: Optional[str], linked_by: Optional[str], query: Optional[str]) -> list:
    conditions = [Model3D.extension.in_(MODEL_EXTENSIONS), Model3D.source_provider.is_not(None)]
    if provider:
        conditions.append(Model3D.source_provider == provider)
    if linked_by:
        conditions.append(Model3D.source_linked_by == linked_by)
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        conditions.append(Model3D.filename.ilike(pattern) | Model3D.source_title.ilike(pattern))
    return conditions


def linked_models(session: Session, provider: Optional[str] = None, linked_by: Optional[str] = None,
                  query: Optional[str] = None, offset: int = 0, limit: int = 20) -> dict:
    """Models that are linked to an online listing, for reviewing / bulk unlinking."""
    conditions = _linked_filter(provider, linked_by, query)
    total = session.exec(select(func.count()).select_from(Model3D).where(*conditions)).one()
    rows = session.exec(select(Model3D).where(*conditions).order_by(Model3D.updated_at.desc(), Model3D.id.desc())
                        .offset(offset).limit(limit)).all()
    return {"total": total, "items": [{
        "id": m.id, "filename": m.filename, "extension": m.extension, "thumbnail_path": m.thumbnail_path,
        "source_provider": m.source_provider, "source_title": m.source_title, "source_url": m.source_url,
        "source_linked_by": m.source_linked_by or "manual", "designer": m.designer,
    } for m in rows]}


def unlink_many(session: Session, model_ids: Optional[list] = None, provider: Optional[str] = None,
                linked_by: Optional[str] = None, query: Optional[str] = None) -> int:
    """Remove the listing link (and the saved pictures) from many models at once.
    Either explicit ids, or every linked model matching the filters."""
    from app.source_linking import unlink_model   # lazy: that module imports this one
    if model_ids is not None:
        models = session.exec(select(Model3D).where(Model3D.id.in_(model_ids), Model3D.source_provider.is_not(None))).all()
    else:
        models = session.exec(select(Model3D).where(*_linked_filter(provider, linked_by, query))).all()
    for model in models:
        unlink_model(session, model)
    return len(models)
