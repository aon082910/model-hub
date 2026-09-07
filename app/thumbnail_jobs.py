import gc
import logging
import threading
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import LIBRARY_PATH, MESH_EXTENSIONS
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
    generate_thumbnail,
    load_mesh,
    normalize_thumbnail_color,
)

logger = logging.getLogger("modelhub.thumbnails")
THUMBNAIL_BATCH_SIZE = 100

_state_lock = threading.Lock()
_job_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "regenerated": 0,
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

    _set_state(running=True, done=0, total=0, regenerated=0, failed=0)
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


def _load_model_page(last_id: int) -> list[tuple[int, str]]:
    """Fetch one bounded page of model ids/paths and close the DB session immediately."""
    with Session(engine) as session:
        rows = session.exec(
            select(Model3D.id, Model3D.path)
            .where(
                Model3D.id > last_id,
                Model3D.extension.in_(MESH_EXTENSIONS),
            )
            .order_by(Model3D.id)
            .limit(THUMBNAIL_BATCH_SIZE)
        ).all()
        return [(int(model_id), str(path)) for model_id, path in rows]


def _persist_thumbnail(model_id: int, thumbnail_path: str) -> None:
    """Update only the cached thumbnail filename for one indexed model."""
    with Session(engine) as session:
        model = session.get(Model3D, model_id)
        if model is None:
            return
        model.thumbnail_path = thumbnail_path
        session.add(model)
        session.commit()


def _run_thumbnail_regeneration() -> None:
    try:
        color, total = _load_job_configuration()
        _set_state(total=total)
        logger.info(
            "Thumbnail regeneration started: %d indexed mesh models, color %s",
            total,
            color,
        )

        last_id = 0
        done = regenerated = failed = 0

        while True:
            page = _load_model_page(last_id)
            if not page:
                break

            for model_id, rel_path in page:
                last_id = model_id
                full_path = LIBRARY_PATH / Path(rel_path)
                thumb_path = None
                mesh = None
                try:
                    if not full_path.exists():
                        raise FileNotFoundError("model file is missing on disk")
                    mesh = load_mesh(full_path)
                    thumb_path = generate_thumbnail(full_path, mesh=mesh, color=color)
                    if not thumb_path:
                        raise ValueError("thumbnail renderer returned no image")
                    _persist_thumbnail(model_id, thumb_path)
                    regenerated += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "Thumbnail regeneration failed for %s: %s",
                        full_path,
                        exc,
                    )
                finally:
                    if mesh is not None:
                        del mesh
                    done += 1
                    _set_state(done=done, regenerated=regenerated, failed=failed)

            gc.collect()
            logger.info(
                "Thumbnail regeneration progress: %d/%d complete (%d failed)",
                done,
                total,
                failed,
            )

        logger.info(
            "Thumbnail regeneration complete: %d regenerated, %d failed",
            regenerated,
            failed,
        )
    except Exception:
        logger.exception("Thumbnail regeneration job failed")
    finally:
        _set_state(running=False)
        release_library_maintenance()
