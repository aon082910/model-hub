import gc
import logging
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures import as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import LIBRARY_PATH, MESH_EXTENSIONS, THUMB_DIR
from app.db import engine
from app.library_maintenance import (
    LibraryMaintenanceBusy,
    acquire_library_maintenance,
    current_library_maintenance,
    release_library_maintenance,
)
from app.mesh_worker import (
    MIN_BUDGET_BYTES,
    WORKER_TIMEOUT_SECONDS,
    MeshWorkerPool,
    mesh_memory_budget_bytes,
    render_thumbnail,
)
from app.models import Model3D
from app.settings_store import get_setting
from app.thumbnails import (
    DEFAULT_THUMBNAIL_COLOR,
    normalize_thumbnail_color,
    thumbnail_filename,
)

logger = logging.getLogger("modelhub.thumbnails")
THUMBNAIL_BATCH_SIZE = 100
THUMBNAIL_WORKERS = max(1, min(8, int(os.environ.get("THUMBNAIL_WORKERS", "4"))))

_state_lock = threading.Lock()
_job_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "regenerated": 0,
    "skipped": 0,
    "failed": 0,
}


def thumbnail_regeneration_status() -> dict:
    with _state_lock:
        return dict(_job_state)


def _set_state(**updates) -> None:
    with _state_lock:
        _job_state.update(updates)


def start_thumbnail_regeneration() -> dict:
    """Start a background thumbnail-only refresh for already indexed mesh models."""
    if not acquire_library_maintenance("thumbnails"):
        active = current_library_maintenance()
        raise LibraryMaintenanceBusy(
            f"Library maintenance already in progress: {active or 'another task'}"
        )

    _set_state(running=True, done=0, total=0, regenerated=0, skipped=0, failed=0)
    worker = threading.Thread(
        target=_run_thumbnail_regeneration,
        name="modelhub-thumbnail-regeneration",
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        _set_state(running=False)
        release_library_maintenance()
        raise
    return thumbnail_regeneration_status()


def _load_job_configuration() -> tuple[str, int]:
    """Read the saved color and eligible model count in a short DB session."""
    with Session(engine) as session:
        color = normalize_thumbnail_color(
            get_setting(session, "viewer_model_color", DEFAULT_THUMBNAIL_COLOR)
        )
        total = session.exec(
            select(func.count())
            .select_from(Model3D)
            .where(Model3D.extension.in_(MESH_EXTENSIONS))
        ).one()
        return color, int(total)


def _load_model_page(last_id: int) -> list[tuple[int, str, str | None]]:
    """Fetch one bounded page of model ids/paths/current thumbnails and close DB quickly."""
    with Session(engine) as session:
        rows = session.exec(
            select(Model3D.id, Model3D.path, Model3D.thumbnail_path)
            .where(
                Model3D.id > last_id,
                Model3D.extension.in_(MESH_EXTENSIONS),
            )
            .order_by(Model3D.id)
            .limit(THUMBNAIL_BATCH_SIZE)
        ).all()
        return [
            (
                int(model_id),
                str(path),
                str(current_thumbnail) if current_thumbnail else None,
            )
            for model_id, path, current_thumbnail in rows
        ]


def _persist_thumbnail_batch(updates: list[tuple[int, str]]) -> None:
    """Persist one page of completed thumbnail filenames in one short transaction."""
    if not updates:
        return
    with Session(engine) as session:
        for model_id, thumbnail_path in updates:
            model = session.get(Model3D, model_id)
            if model is None:
                continue
            model.thumbnail_path = thumbnail_path
            session.add(model)
        session.commit()


def _thumbnail_is_current(full_path: Path, current_thumbnail: str | None, color: str) -> bool:
    """Return True when the DB and cache already point at this render configuration."""
    expected = thumbnail_filename(full_path, color=color)
    return current_thumbnail == expected and (THUMB_DIR / expected).exists()


def _worker_pool() -> MeshWorkerPool:
    """Parallel render workers that together stay within one mesh-worker budget.

    Each parallel worker could otherwise need a full parse's worth of memory at
    once. Splitting the budget means a file too large for one worker's share is
    rendered from its no-parse preview instead (embedded .3mf thumbnail,
    streamed binary STL) rather than several of them OOM-ing the box together.
    """
    budget = mesh_memory_budget_bytes()
    workers = max(1, min(THUMBNAIL_WORKERS, budget // MIN_BUDGET_BYTES))
    return MeshWorkerPool(workers=workers, budget_bytes=budget // workers)


def _run_thumbnail_regeneration() -> None:
    pool = None
    try:
        color, total = _load_job_configuration()
        _set_state(total=total)
        pool = _worker_pool()
        logger.info(
            "Thumbnail regeneration started: %d indexed mesh models, color %s, %d workers x %d MB",
            total,
            color,
            pool.workers,
            pool.budget_bytes // (1024 * 1024),
        )

        last_id = 0
        counts = {"done": 0, "regenerated": 0, "skipped": 0, "failed": 0}

        def record(updates, model_id, full_path, thumbnail=None, error=None):
            if thumbnail:
                updates.append((model_id, thumbnail))
                counts["regenerated"] += 1
            else:
                counts["failed"] += 1
                logger.warning(
                    "Thumbnail regeneration failed for %s: %s",
                    full_path,
                    error or "no thumbnail could be produced",
                )
            counts["done"] += 1
            _set_state(**counts)

        while True:
            page = _load_model_page(last_id)
            if not page:
                break

            last_id = page[-1][0]
            pending = {}
            updates = []

            for model_id, rel_path, current_thumbnail in page:
                full_path = LIBRARY_PATH / Path(rel_path)
                if _thumbnail_is_current(full_path, current_thumbnail, color):
                    counts["skipped"] += 1
                    counts["done"] += 1
                    continue
                try:
                    future = pool.executor().submit(
                        render_thumbnail, str(full_path), color, pool.budget_bytes
                    )
                except BrokenProcessPool:
                    pool.reset()
                    future = pool.executor().submit(
                        render_thumbnail, str(full_path), color, pool.budget_bytes
                    )
                pending[future] = (model_id, full_path)

            # A worker that dies fails every task still queued on that pool, not
            # just its own -- those, and anything left when the page times out
            # (a hung worker), are retried one at a time on fresh workers so the
            # real culprit fails alone instead of taking the rest down with it.
            retry = []
            try:
                for future in as_completed(list(pending), timeout=WORKER_TIMEOUT_SECONDS):
                    model_id, full_path = pending.pop(future)
                    try:
                        record(updates, model_id, full_path, thumbnail=future.result())
                    except BrokenProcessPool:
                        retry.append((model_id, full_path))
                    except Exception as exc:
                        record(updates, model_id, full_path, error=exc)
            except FutureTimeout:
                retry.extend(pending.values())

            if retry:
                pool.reset()
                for model_id, full_path in retry:
                    try:
                        thumbnail = pool.run(render_thumbnail, str(full_path), color, pool.budget_bytes)
                    except Exception as exc:
                        record(updates, model_id, full_path, error=exc)
                    else:
                        record(updates, model_id, full_path, thumbnail=thumbnail)

            # Persist all successful renders from this page in one transaction.
            _persist_thumbnail_batch(updates)
            _set_state(**counts)

            gc.collect()
            logger.info(
                "Thumbnail regeneration progress: %d/%d complete (%d rendered, %d skipped, %d failed)",
                counts["done"],
                total,
                counts["regenerated"],
                counts["skipped"],
                counts["failed"],
            )

        logger.info(
            "Thumbnail regeneration complete: %d rendered, %d skipped, %d failed",
            counts["regenerated"],
            counts["skipped"],
            counts["failed"],
        )
    except Exception:
        logger.exception("Thumbnail regeneration job failed")
    finally:
        if pool is not None:
            pool.shutdown()
        _set_state(running=False)
        release_library_maintenance()
