"""Where the disk space goes: the library by file type, duplicates, the biggest models, and Model Hub's own folders."""
import shutil
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import CONFIG_PATH, LIBRARY_PATH
from app.models import Model3D

CONFIG_FOLDERS = ("thumbnails", "source_images", "print_photos", "print_files", "attachments", "backups", "downloads")


def folder_size(path: Path, limit_entries: int = 200_000) -> int:
    total, seen = 0, 0
    if not path.is_dir():
        return 0
    for p in path.rglob("*"):
        seen += 1
        if seen > limit_entries:
            break
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def disk(path: Path) -> dict:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.parent)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"total": None, "used": None, "free": None}


def build(session: Session) -> dict:
    by_type = session.exec(select(Model3D.extension, func.count(Model3D.id), func.sum(Model3D.size_bytes)).group_by(Model3D.extension)).all()
    total = sum(int(s or 0) for _, _, s in by_type)
    duplicates = session.exec(select(func.count(Model3D.id), func.sum(Model3D.size_bytes)).where(Model3D.is_duplicate_of.is_not(None))).one()
    biggest = session.exec(select(Model3D).order_by(Model3D.size_bytes.desc()).limit(15)).all()
    models = session.exec(select(func.count(Model3D.id))).one()
    return {
        "models": models, "library_bytes": total,
        "by_type": sorted(({"extension": e, "models": n, "bytes": int(s or 0)} for e, n, s in by_type), key=lambda r: -r["bytes"]),
        "duplicates": {"models": duplicates[0] or 0, "bytes": int(duplicates[1] or 0)},
        "biggest": [{"id": m.id, "filename": m.filename, "extension": m.extension, "bytes": m.size_bytes} for m in biggest],
        "folders": [{"name": name, "bytes": folder_size(CONFIG_PATH / name)} for name in CONFIG_FOLDERS],
        "database_bytes": (CONFIG_PATH / "modelhub.db").stat().st_size if (CONFIG_PATH / "modelhub.db").is_file() else 0,
        "library_disk": disk(LIBRARY_PATH), "config_disk": disk(CONFIG_PATH),
    }
