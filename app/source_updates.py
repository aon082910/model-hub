"""Noticing when the online listing of a linked model has changed: a new description,
different pictures, new or changed files, a new license.

A listing is looked at once even when several library models came from it (a pack of
parts). The first look only records what the listing looks like (a fingerprint of its
parts); later looks compare against it. A change stays flagged until you refresh the
model from the listing or dismiss it. The job is as gentle as the matching job: one
listing at a time with a pause, stopping by itself when a site rate limits.
"""
import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app import sources
from app.db import engine
from app.models import Model3D

logger = logging.getLogger("modelhub.sourceupdates")

PARTS = ("title", "description", "tags", "images", "license", "files")
PART_LABELS = {"title": "title", "description": "description", "tags": "tags", "images": "pictures",
               "license": "license", "files": "files"}
PAUSE_SECONDS = 1.0
BACKOFF_SECONDS = 5.0
MAX_CONSECUTIVE_FAILURES = 5
RECHECK_AFTER_HOURS = 20


class JobBusy(Exception):
    pass


_lock = threading.Lock()
_stop = threading.Event()
_state = {"running": False, "started_at": None, "finished_at": None, "total": 0, "checked": 0,
          "changed": 0, "errors": 0, "message": ""}


def _set(**values):
    with _lock:
        _state.update(values)


def _bump(key: str):
    with _lock:
        _state[key] += 1


def job_status() -> dict:
    with _lock:
        return dict(_state)


def _hash(value) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def fingerprint(details: dict, files: Optional[list]) -> dict:
    """A short hash per part of the listing. files is None when the site has no file list we can read."""
    return {
        "title": _hash(details.get("title") or ""),
        "description": _hash(details.get("description") or ""),
        "tags": _hash(sorted(details.get("tags") or [])),
        "images": _hash(details.get("images") or []),
        "license": _hash(details.get("license") or ""),
        "files": None if files is None else _hash(sorted((f.get("name"), f.get("size")) for f in files)),
    }


def current_fingerprint(provider: str, source_id: str, credentials: dict) -> dict:
    from app import downloads
    details = sources.fetch_details(provider, source_id, credentials)
    files = None
    if provider in sources.DOWNLOAD_PROVIDERS:
        try:
            files = downloads.list_files(provider, source_id, credentials)
        except sources.SourceError:
            files = None
    return fingerprint(details, files)


def changed_parts(old: dict, new: dict) -> list:
    """Names of the parts that differ (a part that could not be read either time is not a change)."""
    return [p for p in PARTS if old.get(p) is not None and new.get(p) is not None and old[p] != new[p]]


def _models_of(session: Session, provider: str, source_id: str) -> list:
    return session.exec(select(Model3D).where(Model3D.source_provider == provider, Model3D.source_id == source_id)).all()


def check_listing(session: Session, provider: str, source_id: str, credentials: dict, accept: bool = False) -> str:
    """Look at one listing. Returns 'baseline', 'unchanged' or 'changed'. With accept=True the listing as it
    is now becomes the new normal (and any flag is cleared). Raises SourceError."""
    new = current_fingerprint(provider, source_id, credentials)
    models = _models_of(session, provider, source_id)
    if not models:
        return "unchanged"
    try:
        old = json.loads(models[0].source_fingerprint) if models[0].source_fingerprint else None
    except ValueError:
        old = None
    now = datetime.utcnow()
    if old is None or accept:
        outcome = "baseline"
        for m in models:
            m.source_fingerprint, m.source_change, m.source_changed_at = json.dumps(new), None, None
    else:
        parts = changed_parts(old, new)
        outcome = "changed" if parts else "unchanged"
        for m in models:
            if parts:
                if not m.source_change:
                    m.source_changed_at = now
                m.source_change = json.dumps(parts)
            else:
                m.source_change, m.source_changed_at = None, None
    for m in models:
        m.source_checked_at = now
        session.add(m)
    session.commit()
    return outcome


def refresh_listing(session: Session, provider: str, source_id: str) -> int:
    """Pull the listing's current title, description, tags and pictures into every model linked to it."""
    from app.source_linking import link_model_to_listing
    credentials = sources.load_credentials(session)
    details = sources.fetch_details(provider, source_id, credentials)
    models = _models_of(session, provider, source_id)
    first = None
    for model in models:
        link_model_to_listing(session, model, provider, source_id, images=True, fill_details=False, add_tags=False,
                              linked_by=model.source_linked_by or "manual", details=details, reuse_images_from=first)
        if first is None:
            first = model.id
    check_listing(session, provider, source_id, credentials, accept=True)
    return len(models)


def changed_listings(session: Session) -> list:
    rows = session.exec(select(Model3D).where(Model3D.source_change.is_not(None)).order_by(Model3D.source_changed_at.desc())).all()
    out = {}
    for m in rows:
        entry = out.setdefault((m.source_provider, m.source_id), {
            "provider": m.source_provider, "source_id": m.source_id, "title": m.source_title, "url": m.source_url,
            "label": sources.PROVIDER_LABELS.get(m.source_provider, m.source_provider),
            "changes": [PART_LABELS.get(p, p) for p in json.loads(m.source_change or "[]")],
            "changed_at": m.source_changed_at, "models": []})
        entry["models"].append({"id": m.id, "filename": m.filename})
    return list(out.values())


def _listings_to_check(force: bool) -> list:
    cutoff = datetime.utcnow() - timedelta(hours=RECHECK_AFTER_HOURS)
    with Session(engine) as session:
        rows = session.exec(
            select(Model3D.source_provider, Model3D.source_id, func.min(Model3D.source_checked_at))
            .where(Model3D.source_provider.is_not(None), Model3D.source_id.is_not(None))
            .group_by(Model3D.source_provider, Model3D.source_id)).all()
    due = [(p, i, c) for p, i, c in rows if p in sources.PROVIDERS and (force or c is None or c < cutoff)]
    due.sort(key=lambda r: (r[2] is not None, r[2] or datetime.min))       # never-checked first, then the longest ago
    return [(p, i) for p, i, _ in due]


def run_job(force: bool = False, notify_changes: bool = False) -> None:
    try:
        listings = _listings_to_check(force)
        _set(total=len(listings), message="Checking..." if listings else "Everything was checked recently.")
        failures = 0
        for provider, source_id in listings:
            if _stop.is_set():
                _set(message="Stopped. Start again to continue.")
                return
            try:
                with Session(engine) as session:
                    outcome = check_listing(session, provider, source_id, sources.load_credentials(session))
            except sources.SourceError as e:
                failures += 1
                _bump("errors")
                if "rate limiting" in str(e) or failures >= MAX_CONSECUTIVE_FAILURES:
                    _set(message=("A site is rate limiting requests" if "rate limiting" in str(e) else "The sites keep failing")
                         + ". Stopped; try again in a while.")
                    return
                _stop.wait(BACKOFF_SECONDS)
                continue
            except Exception:
                logger.exception("Update check crashed for %s %s", provider, source_id)
                _bump("errors")
                continue
            failures = 0
            _bump("checked")
            if outcome == "changed":
                _bump("changed")
            _stop.wait(PAUSE_SECONDS)
        _set(message="Finished.")
        if notify_changes and job_status()["changed"]:
            from app.notify import notify_event
            with Session(engine) as session:
                notify_event(session, "listing_changes", "Model Hub: listings changed",
                             f"{job_status()['changed']} linked listing(s) changed. See Matches, Listing updates.")
    except Exception:
        logger.exception("Update job failed")
        _set(message="The job hit an unexpected error; see the container log.")
    finally:
        _set(running=False, finished_at=datetime.utcnow().isoformat())


def start_job(force: bool = False, notify_changes: bool = False) -> dict:
    with _lock:
        if _state["running"]:
            raise JobBusy("A check is already running")
        _state.update(running=True, started_at=datetime.utcnow().isoformat(), finished_at=None,
                      total=0, checked=0, changed=0, errors=0, message="Starting...")
    _stop.clear()
    threading.Thread(target=run_job, args=(force, notify_changes), name="source-updates", daemon=True).start()
    return job_status()


def stop_job() -> dict:
    _stop.set()
    return job_status()
