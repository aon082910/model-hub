import gc
import logging
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
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
from app.models import Model3D
from app.settings_store import get_setting
from app.thumbnails import (
    DEFAULT_THUMBNAIL_COLOR,
    normalize_thumbnail_color,
    render_thumbnail_file,
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


def _render_one(model_id: int, full_path: Path, color: str) -> tuple[int, str]:
    """Render one thumbnail in a worker process and return its DB update payload."""
    if not full_path.exists():
        raise FileNotFoundError(f"model file is missing on disk: {full_path}")
    thumbnail_path = render_thumbnail_file(str(full_path), color)
    if not thumbnail_path:
        raise ValueError("thumbnail renderer returned no image")
    return model_id, thumbnail_path


def _run_thumbnail_regeneration() -> None:
    try:
        color, total = _load_job_configuration()
        _set_state(total=total)
        logger.info(
            "Thumbnail regeneration started: %d indexed mesh models, color %s, %d workers",
            total,
            color,
            THUMBNAIL_WORKERS,
        )

        last_id = 0
        done = regenerated = skipped = failed = 0

        # Spawn avoids forking Python/Matplotlib state from the background thread.
        mp_context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=THUMBNAIL_WORKERS,
            mp_context=mp_context,
        ) as executor:
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
                        skipped += 1
                        done += 1
                        continue

                    future = executor.submit(_render_one, model_id, full_path, color)
                    pending[future] = full_path

                for future in as_completed(pending):
                    full_path = pending[future]
                    try:
                        updates.append(future.result())
                        regenerated += 1
                    except Exception as exc:
                        failed += 1
                        logger.warning(
                            "Thumbnail regeneration failed for %s: %s",
                            full_path,
                            exc,
                        )
                    finally:
                        done += 1
                        _set_state(
                            done=done,
                            regenerated=regenerated,
                            skipped=skipped,
                            failed=failed,
                        )

                # Persist all successful renders from this page in one transaction.
                _persist_thumbnail_batch(updates)
                _set_state(
                    done=done,
                    regenerated=regenerated,
                    skipped=skipped,
                    failed=failed,
                )

                gc.collect()
                logger.info(
                    "Thumbnail regeneration progress: %d/%d complete (%d rendered, %d skipped, %d failed)",
                    done,
                    total,
                    regenerated,
                    skipped,
                    failed,
                )

        logger.info(
            "Thumbnail regeneration complete: %d rendered, %d skipped, %d failed",
            regenerated,
            skipped,
            failed,
        )
    except Exception:
        logger.exception("Thumbnail regeneration job failed")
    finally:
        _set_state(running=False)
        release_library_maintenance()
