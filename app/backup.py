"""Backing up and restoring everything Model Hub knows that is not a model file.

A backup is a zip with a consistent copy of the database (tags, collections,
projects, supplies, links to listings, notes, the wishlist...) and the pictures saved
from listings. The model files themselves live in your library folder and are not
part of it, and neither are thumbnails (they can be rebuilt from Settings).

Passwords, the extension key and every site key or token are blanked in the copy:
a backup file is easy to leave lying around, and restoring should never replace the
logins you have now.

Restoring swaps the content of the data tables for the backup's, inside one
transaction, so a bad file changes nothing. A copy of the current data is saved
first (backups/before-restore-*.zip).
"""
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import SQLModel

from app.auth import RESERVED_SETTING_KEYS
from app.config import CONFIG_PATH, DB_PATH, LIBRARY_PATH
from app.models import AppSettings
from app.routers.prints import PHOTO_ROOT
from app.sources import IMAGE_ROOT, secret_setting_keys

BACKUP_DIR = CONFIG_PATH / "backups"
FORMAT = 1
# folders of pictures that go in a backup: name in the zip -> where they live
PICTURE_FOLDERS = {"source_images": IMAGE_ROOT, "print_photos": PHOTO_ROOT}
SETTINGS_TABLE = AppSettings.__tablename__
MAX_UNPACKED_BYTES = 4 * 1024 ** 3
MAX_FILES = 200_000
KEEP_SAFETY_COPIES = 3
SAVED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip$")


class BackupError(Exception):
    """The file is not a usable backup, or it can't be restored right now."""


def sensitive_settings() -> set:
    return set(RESERVED_SETTING_KEYS) | {"ai_api_key"} | secret_setting_keys()


def _stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def _data_tables(conn: sqlite3.Connection, schema: str = "main") -> list:
    rows = conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    known = set(SQLModel.metadata.tables)
    return [r[0] for r in rows if r[0] in known]


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list:
    return [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")').fetchall()]


def make_backup(destination: Path, include_images: bool = True) -> dict:
    """Write a backup zip to `destination`; returns what went into it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CONFIG_PATH) as tmp:
        copy_path = Path(tmp) / "modelhub.db"
        source = sqlite3.connect(DB_PATH, timeout=30)
        target = sqlite3.connect(copy_path)
        try:
            source.backup(target)              # a consistent copy even while the app is writing
            marks = ",".join("?" for _ in sensitive_settings())
            target.execute(f'DELETE FROM "{SETTINGS_TABLE}" WHERE key IN ({marks})', tuple(sensitive_settings()))
            target.commit()
            counts = {t: target.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _data_tables(target)}
            target.execute("VACUUM")
        finally:
            target.close()
            source.close()
        images = 0
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(copy_path, "modelhub.db")
            if include_images:
                for name, root in PICTURE_FOLDERS.items():
                    for path in sorted(root.rglob("*")) if root.is_dir() else []:
                        if path.is_file():
                            archive.write(path, f"{name}/" + path.relative_to(root).as_posix())
                            images += 1
            manifest = {"format": FORMAT, "created": datetime.utcnow().isoformat() + "Z",
                        "tables": counts, "pictures": images}
            archive.writestr("manifest.json", json.dumps(manifest, indent=1))
    return manifest


# ---------- saved copies on the server ----------

def saved_backups() -> list:
    if not BACKUP_DIR.is_dir():
        return []
    out = []
    for path in sorted(BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        if SAVED_NAME.match(path.name):
            stat = path.stat()
            out.append({"name": path.name, "size": stat.st_size,
                        "created": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z"})
    return out


def saved_path(name: str) -> Path:
    if not SAVED_NAME.match(name or ""):
        raise BackupError("That is not a saved backup")
    path = BACKUP_DIR / name
    if not path.is_file():
        raise BackupError("That saved backup no longer exists")
    return path


def save_backup(prefix: str = "modelhub-backup") -> dict:
    path = BACKUP_DIR / f"{prefix}-{_stamp()}.zip"
    manifest = make_backup(path)
    if prefix == "before-restore":
        for old in [b for b in saved_backups() if b["name"].startswith("before-restore-")][KEEP_SAFETY_COPIES:]:
            (BACKUP_DIR / old["name"]).unlink(missing_ok=True)
    return {"name": path.name, "size": path.stat().st_size, **manifest}


# ---------- restoring ----------

def _safe_member(name: str) -> bool:
    if name in ("modelhub.db", "manifest.json"):
        return True
    if not any(name.startswith(f"{folder}/") for folder in PICTURE_FOLDERS) or name.endswith("/"):
        return False
    parts = name.split("/")
    return ".." not in parts and "" not in parts[1:] and "\\" not in name and not name.startswith("/") and len(parts) <= 4


def _unpack(zip_path: Path, folder: Path) -> dict:
    """Extract a backup into `folder`, refusing anything that is not what make_backup writes."""
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise BackupError("That file is not a zip, so it is not a Model Hub backup")
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if not any(i.filename == "modelhub.db" for i in infos):
            raise BackupError("That zip has no modelhub.db, so it is not a Model Hub backup")
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise BackupError("That backup is larger than this server will restore")
        for info in infos:
            if not _safe_member(info.filename):
                raise BackupError(f"The backup contains a file it should not ({info.filename[:60]})")
        manifest = {}
        for info in infos:
            target = folder / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        try:
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if manifest.get("format", FORMAT) > FORMAT:
        raise BackupError("That backup was made by a newer version of Model Hub; update Model Hub first")
    return manifest


def _swap_tables(backup_db: Path) -> dict:
    """Replace the data tables with the backup's, atomically. Settings are merged, never replaced."""
    check = sqlite3.connect(backup_db)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("The database inside that backup is damaged")
        if "model3d" not in {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            raise BackupError("The database inside that backup is not a Model Hub database")
    except sqlite3.DatabaseError:
        raise BackupError("The database inside that backup can't be read")
    finally:
        check.close()

    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    restored = {}
    try:
        conn.execute("ATTACH DATABASE ? AS bk", (str(backup_db),))
        conn.execute("BEGIN IMMEDIATE")
        try:
            backup_tables = set(_data_tables(conn, "bk"))
            for table in _data_tables(conn):
                if table == SETTINGS_TABLE:
                    continue
                conn.execute(f'DELETE FROM main."{table}"')
                if table in backup_tables:
                    shared = [c for c in _columns(conn, table) if c in set(_columns(conn, table, "bk"))]
                    cols = ",".join(f'"{c}"' for c in shared)
                    conn.execute(f'INSERT INTO main."{table}" ({cols}) SELECT {cols} FROM bk."{table}"')
                restored[table] = conn.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
            if SETTINGS_TABLE in backup_tables:
                skip = sensitive_settings()
                for key, value in conn.execute(f'SELECT key, value FROM bk."{SETTINGS_TABLE}"').fetchall():
                    if key in skip:
                        continue
                    updated = conn.execute(f'UPDATE main."{SETTINGS_TABLE}" SET value=? WHERE key=?', (value, key)).rowcount
                    if not updated:
                        conn.execute(f'INSERT INTO main."{SETTINGS_TABLE}" (key, value) VALUES (?, ?)', (key, value))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("DETACH DATABASE bk")
    finally:
        conn.close()
    return restored


def restore_backup(zip_path: Path) -> dict:
    """Restore from a backup zip. Raises BackupError (nothing changed) if it can't."""
    from app.library_maintenance import acquire_library_maintenance, current_library_maintenance, release_library_maintenance
    if not acquire_library_maintenance("restore"):
        raise BackupError(f"Another task is running ({current_library_maintenance() or 'maintenance'}); try again when it has finished")
    try:
        _refuse_while_busy()
        with tempfile.TemporaryDirectory(dir=CONFIG_PATH) as tmp:
            folder = Path(tmp)
            manifest = _unpack(zip_path, folder)
            safety = save_backup("before-restore")
            try:
                restored = _swap_tables(folder / "modelhub.db")
            except sqlite3.DatabaseError as e:
                raise BackupError(f"The backup could not be restored ({e.__class__.__name__}); nothing was changed")
            pictures = 0
            for name, root in PICTURE_FOLDERS.items():
                unpacked = folder / name
                if root.exists():
                    shutil.rmtree(root, ignore_errors=True)
                if unpacked.is_dir():
                    shutil.move(str(unpacked), str(root))
                    pictures += sum(1 for p in root.rglob("*") if p.is_file())
        return {"restored": restored, "pictures": pictures, "safety_copy": safety["name"],
                "created": manifest.get("created"), "missing_files": count_missing_files()}
    finally:
        release_library_maintenance()


def _refuse_while_busy() -> None:
    from app import downloads, source_match_jobs
    if any(i["status"] in ("queued", "downloading", "importing") for i in downloads.snapshot()["items"]):
        raise BackupError("Downloads are still running; let them finish (or cancel them) first")
    if source_match_jobs.job_status().get("running"):
        raise BackupError("The library matching job is running; stop it first")


def count_missing_files() -> int:
    """Models in the index whose file is not in the library folder (they are dropped by the next scan)."""
    from sqlmodel import Session, select
    from app.db import engine
    from app.models import Model3D
    with Session(engine) as session:
        return sum(1 for path in session.exec(select(Model3D.path)).all() if not (LIBRARY_PATH / path).exists())
