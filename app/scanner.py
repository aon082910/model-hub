import gc
import hashlib
import json
import logging
from pathlib import Path
from datetime import datetime

from sqlmodel import Session, select

from app.config import CONFIG_PATH, LIBRARY_PATH, SUPPORTED_EXTENSIONS, MESH_EXTENSIONS
from app.library_maintenance import (
    acquire_library_maintenance,
    current_library_maintenance,
    release_library_maintenance,
)
from app.mesh_worker import MeshWorkerError, analyze_file, preview_file, scan_worker_pool
from app.models import Model3D
from app.settings_store import get_setting
from app.thumbnails import DEFAULT_THUMBNAIL_COLOR, normalize_thumbnail_color

logger = logging.getLogger("modelhub.scanner")

SCAN_BATCH_SIZE = 100

# Last-resort crash-loop breaker. Mesh parsing already runs in a memory-capped
# worker (app/mesh_worker.py) so a single file shouldn't be able to take the
# server down -- but if something unanticipated still does, the background
# scan restarts on the next launch and walks straight back into the same file.
# The path being analyzed is recorded here and cleared once it's committed;
# a marker still present at the start of a scan names the file the previous
# scan died on. Two strikes (not one, so an ordinary restart mid-scan doesn't
# penalize an innocent file) and the file is indexed without mesh analysis
# from then on.
INFLIGHT_MARKER = CONFIG_PATH / "scan_inflight.json"
CRASH_STRIKES_BEFORE_SKIP = 2


def _load_crash_suspects() -> dict:
    try:
        data = json.loads(INFLIGHT_MARKER.read_text())
        INFLIGHT_MARKER.unlink(missing_ok=True)
        path, strikes = data["path"], int(data.get("strikes", 0)) + 1
    except FileNotFoundError:
        return {}
    except Exception:
        INFLIGHT_MARKER.unlink(missing_ok=True)
        return {}
    logger.warning(
        "The previous library scan ended while analyzing %s (%d time(s) in a row)", path, strikes
    )
    return {path: strikes}


def _write_inflight_marker(rel_path: str, strikes: int) -> None:
    try:
        INFLIGHT_MARKER.write_text(json.dumps({"path": rel_path, "strikes": strikes}))
    except OSError:
        pass


def _clear_inflight_marker() -> None:
    try:
        INFLIGHT_MARKER.unlink(missing_ok=True)
    except OSError:
        pass


class ScanAlreadyRunning(RuntimeError):
    """Raised when another library maintenance task prevents a scan from starting."""


def scan_is_running() -> bool:
    """Return whether the process-wide library maintenance slot is owned by a scan."""
    return current_library_maintenance() == "scan"


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _upsert_path(
    session: Session, path: Path, rel_path: str, counters: dict, crash_suspects: dict = None
) -> Model3D:
    """Hash/measure/thumbnail a single file on disk and create-or-update its Model3D row.
    Shared by the directory scanner and the browser-extension import endpoint.

    Expensive filesystem/mesh work is intentionally performed outside an active
    database transaction so the background scanner does not block unrelated SQLite
    writes such as queue, filament, or metadata updates.

    crash_suspects (scans only) enables the in-flight crash-loop marker; see
    INFLIGHT_MARKER.
    """
    ext = path.suffix.lower()
    stat = path.stat()
    existing = session.exec(select(Model3D).where(Model3D.path == rel_path)).first()

    if existing and existing.size_bytes == stat.st_size:
        existing.last_scanned_at = datetime.utcnow()
        session.add(existing)
        return existing

    # Capture the saved thumbnail color while the initial lookup transaction is
    # still open, then release that transaction before hashing/mesh/render work.
    thumbnail_color = normalize_thumbnail_color(
        get_setting(session, "viewer_model_color", DEFAULT_THUMBNAIL_COLOR)
    )

    # The lookup above starts a SQLite read transaction. End it before hashing,
    # Trimesh analysis, or thumbnail rendering, all of which can take seconds or
    # minutes on detailed files. Capture only the identity we need and re-fetch the
    # row when the completed metadata is ready to persist.
    existing_id = existing.id if existing else None
    session.rollback()

    content_hash = hash_file(path)
    geometry_hash = None
    vcount = fcount = None
    bbox = (None, None, None)
    volume_mm3 = None
    is_watertight = None
    thumb_path = None

    if ext in MESH_EXTENSIONS:
        strikes = crash_suspects.get(rel_path, 0) if crash_suspects is not None else 0
        if strikes >= CRASH_STRIKES_BEFORE_SKIP:
            logger.warning(
                "Skipping mesh analysis for %s: library scans ended while analyzing it %d times "
                "in a row. It is indexed without stats or a thumbnail.",
                rel_path, strikes,
            )
        else:
            if crash_suspects is not None:
                _write_inflight_marker(rel_path, strikes)
            pool = scan_worker_pool()
            analysis = None
            try:
                # Never parsed in this process -- see app/mesh_worker.py.
                analysis = pool.run(analyze_file, str(path), thumbnail_color, pool.budget_bytes)
            except MeshWorkerError as e:
                logger.warning("Mesh analysis failed for %s: %s", rel_path, e)
                try:
                    analysis = pool.run(preview_file, str(path), thumbnail_color)
                except Exception as e2:
                    logger.warning("Preview fallback also failed for %s: %s", rel_path, e2)
            except Exception as e:
                logger.warning("Mesh analysis failed for %s: %s", rel_path, e)
            if analysis is not None:
                for problem in analysis.get("errors", []):
                    logger.warning("%s: %s", rel_path, problem)
                geometry_hash = analysis["geometry_hash"]
                vcount = analysis["vertex_count"]
                fcount = analysis["face_count"]
                bbox = tuple(analysis["bbox"])
                volume_mm3 = analysis["volume_mm3"]
                is_watertight = analysis["is_watertight"]
                thumb_path = analysis["thumbnail_path"]

    # From here to the caller's commit, keep DB work short: duplicate lookup,
    # re-fetch/update (if needed), and persistence only.
    dup_of = None
    dup_match = session.exec(
        select(Model3D).where(Model3D.content_hash == content_hash, Model3D.path != rel_path)
    ).first()
    if dup_match:
        dup_of = dup_match.id
        counters["duplicates"] = counters.get("duplicates", 0) + 1

    existing = session.get(Model3D, existing_id) if existing_id is not None else None
    if existing:
        existing.size_bytes = stat.st_size
        existing.content_hash = content_hash
        existing.geometry_hash = geometry_hash
        existing.vertex_count = vcount
        existing.face_count = fcount
        existing.bbox_x, existing.bbox_y, existing.bbox_z = bbox
        existing.volume_mm3 = volume_mm3
        existing.is_watertight = is_watertight
        existing.thumbnail_path = thumb_path or existing.thumbnail_path
        existing.is_duplicate_of = dup_of
        existing.updated_at = datetime.utcnow()
        existing.last_scanned_at = datetime.utcnow()
        session.add(existing)
        counters["updated"] = counters.get("updated", 0) + 1
        return existing

    model = Model3D(
        filename=path.name,
        path=rel_path,
        extension=ext,
        size_bytes=stat.st_size,
        content_hash=content_hash,
        geometry_hash=geometry_hash,
        vertex_count=vcount,
        face_count=fcount,
        bbox_x=bbox[0], bbox_y=bbox[1], bbox_z=bbox[2],
        volume_mm3=volume_mm3,
        is_watertight=is_watertight,
        thumbnail_path=thumb_path,
        is_duplicate_of=dup_of,
    )
    session.add(model)
    counters["added"] = counters.get("added", 0) + 1
    return model


def _checkpoint_session(session: Session) -> None:
    """Release ORM state and collect garbage at a bounded scan interval."""
    session.expunge_all()
    # Mesh parsing/rendering can leave large cyclic Python object graphs behind.
    # Collect at the same bounded checkpoint so long scans do not accumulate them.
    gc.collect()


def _remove_missing_models(session: Session) -> None:
    """Remove stale DB rows in bounded batches instead of loading the full table."""
    last_id = 0
    while True:
        models = session.exec(
            select(Model3D)
            .where(Model3D.id > last_id)
            .order_by(Model3D.id)
            .limit(SCAN_BATCH_SIZE)
        ).all()
        if not models:
            break

        last_id = models[-1].id
        for model in models:
            if not (LIBRARY_PATH / model.path).exists():
                session.delete(model)

        # Stale cleanup does no expensive work after records are marked for
        # deletion, so one short commit per bounded cleanup page is sufficient.
        session.commit()
        _checkpoint_session(session)


def scan_library(session: Session) -> dict:
    """Run one library scan, rejecting overlapping heavy library maintenance."""
    if not acquire_library_maintenance("scan"):
        active = current_library_maintenance()
        if active == "scan":
            message = "Library scan already in progress"
        else:
            message = f"Library maintenance already in progress: {active or 'another task'}"
        raise ScanAlreadyRunning(message)
    try:
        return _scan_library(session)
    finally:
        release_library_maintenance()


def _scan_library(session: Session) -> dict:
    """Walk LIBRARY_PATH, add new files, update changed ones, flag duplicates."""
    counters = {"found": 0, "added": 0, "updated": 0, "duplicates": 0}

    if not LIBRARY_PATH.exists():
        logger.warning("Library path %s does not exist", LIBRARY_PATH)
        return counters

    logger.info("Library scan started: %s", LIBRARY_PATH)
    crash_suspects = _load_crash_suspects()

    for path in LIBRARY_PATH.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        counters["found"] += 1
        rel_path = str(path.relative_to(LIBRARY_PATH))
        _upsert_path(session, path, rel_path, counters, crash_suspects=crash_suspects)

        # Persist each completed file immediately. This releases SQLite's writer
        # lock between models instead of holding it while the next model is hashed,
        # parsed, and rendered. Memory cleanup remains batched separately below.
        session.commit()
        _clear_inflight_marker()

        if counters["found"] % SCAN_BATCH_SIZE == 0:
            _checkpoint_session(session)
            logger.info(
                "Library scan progress: %d models processed; current: %s",
                counters["found"],
                rel_path,
            )

    # Release ORM state for the final partial batch (or a small library below the
    # configured checkpoint size). Every model has already been committed above.
    if counters["found"] % SCAN_BATCH_SIZE:
        _checkpoint_session(session)

    _remove_missing_models(session)

    logger.info("Library scan complete: %s", counters)
    return counters


def import_uploaded_file(
    session: Session,
    filename: str,
    content: bytes,
    source_url: str = None,
    designer: str = None,
    license: str = None,
) -> Model3D:
    """Save bytes pushed in by the browser extension (or any client) into the
    library under an 'imported/' subfolder, then process it like any scanned file.
    """
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")

    import_dir = LIBRARY_PATH / "imported"
    import_dir.mkdir(parents=True, exist_ok=True)

    dest = import_dir / filename
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while dest.exists():
        dest = import_dir / f"{stem} ({n}){suffix}"
        n += 1
    dest.write_bytes(content)

    counters = {}
    rel_path = str(dest.relative_to(LIBRARY_PATH))
    model = _upsert_path(session, dest, rel_path, counters)

    if source_url:
        model.source_url = source_url
    if designer:
        model.designer = designer
    if license:
        model.license = license
    session.add(model)
    session.commit()
    session.refresh(model)
    return model
