"""Copy backups somewhere else: a folder (a mounted share, a USB disk, another server's share) or a WebDAV server (Nextcloud, ownCloud,
a NAS). Off until configured. The newest saved backup is sent once; the password is never put in a response or a log."""
import logging
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
from sqlmodel import Session

from app import backup
from app.settings_store import get_setting, set_setting

logger = logging.getLogger("modelhub.offsite")

KINDS = ("folder", "webdav")
BACKUP_NAME = re.compile(r"^(modelhub-backup|auto-backup)-[A-Za-z0-9._-]+\.zip$")
DEFAULT_KEEP = 10


class OffsiteError(Exception):
    pass


def settings(session: Session) -> Optional[dict]:
    kind = (get_setting(session, "offsite_kind", "") or "").strip()
    if kind not in KINDS:
        return None
    try:
        keep = max(1, min(1000, int(get_setting(session, "offsite_keep", "") or DEFAULT_KEEP)))
    except ValueError:
        keep = DEFAULT_KEEP
    return {"kind": kind, "path": (get_setting(session, "offsite_path", "") or "").strip(), "url": (get_setting(session, "offsite_url", "") or "").strip(),
            "user": get_setting(session, "offsite_user", "") or "", "password": get_setting(session, "offsite_password", "") or "", "keep": keep}


def _folder(cfg: dict) -> Path:
    raw = cfg["path"]
    if not raw or ".." in Path(raw).parts or not Path(raw).is_absolute():
        raise OffsiteError("Give the folder as a full path, like /offsite or D:\\Backups (no ..)")
    folder = Path(raw)
    if not folder.is_dir():
        raise OffsiteError("That folder does not exist inside the container (add it as a path mapping, then enter the container's path)")
    return folder


def _webdav_url(cfg: dict, name: str) -> str:
    parsed = urlparse(cfg["url"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OffsiteError("The WebDAV address must be http(s)://host/path, with the user name and password entered separately")
    return cfg["url"].rstrip("/") + "/" + quote(name)


def send_file(cfg: dict, path: Path) -> str:
    """Put one file at the destination. Returns where it went (no secrets)."""
    if cfg["kind"] == "folder":
        folder = _folder(cfg)
        target = folder / path.name
        part = folder / (path.name + ".part")
        try:
            shutil.copy2(path, part)
            part.replace(target)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise OffsiteError(f"Could not write to the folder ({e.__class__.__name__})")
        mine = sorted((p for p in folder.iterdir() if BACKUP_NAME.match(p.name)), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in mine[cfg["keep"]:]:                              # only ever Model Hub's own backup files, only beyond the newest few
            try:
                old.unlink()
            except OSError:
                pass
        return str(target)
    url = _webdav_url(cfg, path.name)
    try:
        with open(path, "rb") as handle:
            response = httpx.put(url, content=handle, auth=(cfg["user"], cfg["password"]) if cfg["user"] else None,
                                 timeout=httpx.Timeout(30.0, read=600.0, write=600.0), follow_redirects=False)
    except httpx.HTTPError as e:
        raise OffsiteError(f"Could not reach the WebDAV server ({e.__class__.__name__})")
    if response.status_code in (401, 403):
        raise OffsiteError("The WebDAV server refused the user name or password")
    if response.status_code not in (200, 201, 204):
        raise OffsiteError(f"The WebDAV server answered with an error ({response.status_code})")
    return f"{urlparse(url).netloc}/{path.name}"


def newest_backup() -> Optional[Path]:
    if not backup.BACKUP_DIR.is_dir():
        return None
    mine = [p for p in backup.BACKUP_DIR.glob("*.zip") if BACKUP_NAME.match(p.name)]
    return max(mine, key=lambda p: p.stat().st_mtime) if mine else None


def run(session: Session, force: bool = False) -> Optional[str]:
    """Send the newest saved backup if it has not been sent yet (or always with force). Returns where it went, or None."""
    cfg = settings(session)
    if not cfg:
        return None
    newest = newest_backup()
    if not newest or (not force and get_setting(session, "offsite_last", "") == newest.name):
        return None
    where = send_file(cfg, newest)
    set_setting(session, "offsite_last", newest.name)
    set_setting(session, "offsite_last_error", "")
    return where


def scheduled(session: Session) -> None:
    try:
        run(session)
    except OffsiteError as e:
        set_setting(session, "offsite_last_error", str(e))
        logger.info("Off-site backup failed: %s", e)
    except Exception as e:                       # never let a job stop the others
        set_setting(session, "offsite_last_error", f"{e.__class__.__name__}")
        logger.warning("Off-site backup failed: %s", e.__class__.__name__)


def test(session: Session) -> str:
    """Write a tiny file to the destination, to check the settings."""
    import tempfile
    cfg = settings(session)
    if not cfg:
        raise OffsiteError("Choose where to send the backups first")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "modelhub-offsite-test.txt"
        probe.write_text("Model Hub can write here.\n")
        where = send_file(cfg, probe)
    return where
