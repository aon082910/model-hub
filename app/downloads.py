"""Download a listing's model files into the library (Printables, Thingiverse,
Wikimedia Commons, NASA 3D Resources and the Smithsonian's 3D models).

Each listing is one item in a small background queue. For an item the worker
fetches the listing's details, lists its files, downloads the model files to a
temporary folder, imports them into library/imported/<Site>/<title> [<id>]/
(only model file types are kept; a zip is opened and just its models imported),
and links every imported model to the listing so it arrives with its picture,
designer, license and tags.

Safety: every request, including each redirect hop, must be https to the site's
own domains; the access token is only ever sent to Thingiverse's own hosts; a
download stops past a size cap; and nothing but model files is written into the
library.
"""
import logging
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx
from sqlmodel import Session, select

from app import sources
from app.config import ARCHIVE_EXTENSIONS, CONFIG_PATH, LIBRARY_PATH, MODEL_EXTENSIONS
from app.db import engine
from app.models import Model3D
from app.scanner import import_archive_from_path, import_model_from_path

logger = logging.getLogger("modelhub.downloads")

MAX_DOWNLOAD_BYTES = 1536 * 1024 ** 2          # one file
MAX_FILES_PER_LISTING = 40
MAX_REDIRECTS = 5
MAX_KEPT_ITEMS = 300                           # finished items stay listed until cleared, up to this many
TEMP_ROOT = CONFIG_PATH / "downloads"
THINGIVERSE_AUTH_HOSTS = ("api.thingiverse.com", "www.thingiverse.com", "thingiverse.com")


class Cancelled(Exception):
    pass


def _thingiverse_host_ok(host: Optional[str]) -> bool:
    host = (host or "").lower()
    return sources.host_in_domains(host, ("thingiverse.com",)) or (
        host.startswith("thingiverse") and host.endswith(".amazonaws.com"))


def _printables_host_ok(host: Optional[str]) -> bool:
    return sources.host_in_domains(host, ("printables.com",))


def _commons_host_ok(host: Optional[str]) -> bool:
    return sources.host_in_domains(host, ("wikimedia.org",))


def _nasa_host_ok(host: Optional[str]) -> bool:
    return sources.host_in_domains(host, ("githubusercontent.com",))


def _smithsonian_host_ok(host: Optional[str]) -> bool:
    return sources.host_in_domains(host, sources.SMITHSONIAN_DOMAINS)


def _archive_host_ok(host: Optional[str]) -> bool:
    return sources.host_in_domains(host, sources.ARCHIVE_DOMAINS)


HOST_CHECKS = {
    "printables": _printables_host_ok, "thingiverse": _thingiverse_host_ok,
    "commons": _commons_host_ok, "nasa3d": _nasa_host_ok, "smithsonian": _smithsonian_host_ok, "archive": _archive_host_ok,
}


def safe_name(text: str, fallback: str = "listing") -> str:
    cleaned = re.sub(r"[^\w\-. ()\[\]]+", "", text or "", flags=re.UNICODE).strip(" .")
    return cleaned[:80] or fallback


def _filename_from_response(response: httpx.Response, fallback: str) -> str:
    disposition = response.headers.get("content-disposition") or ""
    match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I) or re.search(r'filename="?([^";]+)"?', disposition, re.I)
    name = unquote(match.group(1)).strip().strip('"') if match else ""
    return Path(name).name or Path(urlparse(str(response.url)).path).name or fallback


def download_file(
    client: httpx.Client, url: str, dest_dir: Path, fallback_name: str, host_ok: Callable,
    auth_header: Optional[str] = None, auth_hosts: tuple = (),
    progress: Optional[Callable[[int, int], None]] = None, cancelled: Optional[Callable[[], bool]] = None,
) -> Path:
    """Stream one file to dest_dir and return its path. Follows redirects by hand
    so every hop can be checked."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme != "https" or not host_ok(parsed.hostname):
            raise sources.SourceError("The site pointed to a download on an unexpected address; it was not fetched")
        headers = {"Authorization": auth_header} if auth_header and (parsed.hostname or "").lower() in auth_hosts else {}
        try:
            with client.stream("GET", current, headers=headers, timeout=httpx.Timeout(60.0, connect=15.0)) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise sources.SourceError("The download redirected without saying where")
                    current = urljoin(current, location)
                    continue
                if response.status_code in (401, 403):
                    raise sources.SourceError("The site refused the download (it may need a login or a different token)")
                if response.status_code == 404:
                    raise sources.SourceError("The file was not found on the site")
                if response.status_code == 429:
                    raise sources.SourceError("The site is rate limiting downloads; try again in a few minutes")
                if response.status_code != 200:
                    raise sources.SourceError(f"The download failed ({response.status_code})")
                total = int(response.headers.get("content-length") or 0)
                if total > MAX_DOWNLOAD_BYTES:
                    raise sources.SourceError(f"That file is larger than the {MAX_DOWNLOAD_BYTES // 1024 ** 2} MB limit")
                name = safe_name(_filename_from_response(response, fallback_name), "download")
                path = dest_dir / name
                done = 0
                with open(path, "wb") as target:
                    for chunk in response.iter_bytes(1024 * 256):
                        if cancelled and cancelled():
                            raise Cancelled()
                        done += len(chunk)
                        if done > MAX_DOWNLOAD_BYTES:
                            raise sources.SourceError(f"That file is larger than the {MAX_DOWNLOAD_BYTES // 1024 ** 2} MB limit")
                        target.write(chunk)
                        if progress:
                            progress(done, total)
                return path
        except httpx.HTTPError as e:
            raise sources.SourceError(f"The download failed ({e.__class__.__name__})")
    raise sources.SourceError("The download redirected too many times")


# ---------- which files to fetch ----------

def plan_thingiverse_files(files: list) -> list:
    """Model files first; if a thing only offers zips, take those."""
    models = [f for f in files if Path(f["name"]).suffix.lower() in MODEL_EXTENSIONS]
    if models:
        return models[:MAX_FILES_PER_LISTING]
    return [f for f in files if Path(f["name"]).suffix.lower() in ARCHIVE_EXTENSIONS][:MAX_FILES_PER_LISTING]


def plan_printables_files(files: list) -> list:
    """The 'all model files' pack when there is one, otherwise the individual STLs."""
    packs = [f for f in files if f["kind"] == "pack"]
    return packs[:1] if packs else [f for f in files if f["kind"] == "stl"][:MAX_FILES_PER_LISTING]


def provider_files(client: httpx.Client, provider: str, source_id: str, credentials: Optional[dict]) -> list:
    """Every file this server could download for a listing, each as {id, name,
    size, kind, selectable, selected, ...}. selectable: it is a model file (or a
    zip that may contain some); selected: part of the default choice."""
    if provider == "printables":
        files = sources.printables_files(client, source_id)
        chosen = {f["id"] for f in plan_printables_files(files)}
        return [{**f, "selectable": True, "selected": f["id"] in chosen} for f in files]
    if provider == "thingiverse":
        files = sources.thingiverse_files(client, source_id, sources._thingiverse_token(credentials))
        chosen = {f["id"] or f["name"] for f in plan_thingiverse_files(files)}
        out = []
        for f in files:
            suffix = Path(f["name"]).suffix.lower()
            out.append({**f, "id": f["id"] or f["name"], "kind": suffix.lstrip("."),
                        "selectable": suffix in MODEL_EXTENSIONS or suffix in ARCHIVE_EXTENSIONS,
                        "selected": (f["id"] or f["name"]) in chosen})
        return out
    if provider == "commons":
        files = sources.commons_files(client, source_id)
    elif provider == "nasa3d":
        files = sources.nasa_files(client, source_id)
    elif provider == "archive":
        files = sources.archive_files(client, source_id)
    elif provider == "smithsonian":
        files = sources.smithsonian_files(client, source_id)
        ready = [f for f in files if f["print_ready"]]
        if ready:
            chosen = {f["id"] for f in ready}
        else:                                           # no STL: the smallest OBJ is better than nothing
            smallest = min(files, key=lambda f: f.get("size") or 0, default=None)
            chosen = {smallest["id"]} if smallest else set()
        return [{**f, "kind": Path(f["name"]).suffix.lstrip(".").lower(), "selectable": True, "selected": f["id"] in chosen} for f in files]
    else:
        return []
    return [{**f, "kind": Path(f["name"]).suffix.lstrip(".").lower(), "selectable": True, "selected": True} for f in files]


def list_files(provider: str, source_id: str, credentials: Optional[dict]) -> list:
    """The listing's files for its page (no internal fields): [{id, name, size, kind, selectable, selected}]."""
    if provider not in sources.DOWNLOAD_PROVIDERS:
        return []
    with sources._client() as client:
        files = provider_files(client, provider, source_id, credentials)
    return [{k: f.get(k) for k in ("id", "name", "size", "kind", "selectable", "selected")} for f in files]


def choose_files(files: list, file_ids: Optional[list]) -> list:
    """The files to download: the ones asked for (only those that can be), else the default choice."""
    if file_ids:
        wanted = {str(i) for i in file_ids}
        chosen = [f for f in files if str(f["id"]) in wanted and f["selectable"]]
        if not chosen:
            raise sources.SourceError("None of the chosen files can be downloaded")
    else:
        chosen = [f for f in files if f["selected"]]
    return chosen[:MAX_FILES_PER_LISTING]


# ---------- one listing, start to finish ----------

def download_listing(session: Session, item: dict, credentials: Optional[dict],
                     progress: Callable[[int, int], None], cancelled: Callable[[], bool],
                     set_status: Callable[[str, str], None]) -> list:
    """Download and import one listing; returns the ids of the models it added."""
    provider, source_id = item["provider"], item["source_id"]
    if provider not in sources.DOWNLOAD_PROVIDERS:
        raise sources.SourceError(sources.DOWNLOAD_NOTES.get(provider, "Downloads are not available for this site"))

    details = sources.fetch_details(provider, source_id, credentials)
    host_ok = HOST_CHECKS[provider]
    folder = LIBRARY_PATH / "imported" / sources.PROVIDER_LABELS[provider] / f"{safe_name(details['title'])} [{source_id}]"
    temp = TEMP_ROOT / item["id"]
    models, ignored = [], 0
    try:
        with sources._client() as client:
            set_status("downloading", "Finding the model files...")
            files = choose_files(provider_files(client, provider, source_id, credentials), item.get("file_ids"))
            if not files:
                raise sources.SourceError("This listing has no model files to download (only print files or none at all)")

            for index, f in enumerate(files, start=1):
                if cancelled():
                    raise Cancelled()
                set_status("downloading", f"Downloading {f['name']} ({index} of {len(files)})")
                if provider == "printables":
                    url = sources.printables_download_link(client, source_id, f["id"], f["kind"])
                    path = download_file(client, url, temp, f["name"], host_ok, progress=progress, cancelled=cancelled)
                elif provider == "thingiverse":
                    token = sources._thingiverse_token(credentials)
                    path = download_file(client, f["url"], temp, f["name"], host_ok, auth_header=f"Bearer {token}",
                                         auth_hosts=THINGIVERSE_AUTH_HOSTS, progress=progress, cancelled=cancelled)
                else:
                    path = download_file(client, f["url"], temp, f["name"], host_ok, progress=progress, cancelled=cancelled)
                set_status("importing", f"Adding {path.name} to the library")
                suffix = path.suffix.lower()
                try:
                    if suffix in ARCHIVE_EXTENSIONS:
                        result = import_archive_from_path(session, path, folder, source_url=details["url"])
                        models += result["models"]
                        ignored += result["ignored"]
                    elif suffix in MODEL_EXTENSIONS:
                        models.append(import_model_from_path(session, path, folder, path.name, source_url=details["url"]))
                    else:
                        ignored += 1
                except ValueError as e:
                    raise sources.SourceError(str(e))
        if not models:
            raise sources.SourceError("None of the downloaded files were model files")

        set_status("importing", "Linking the listing's details")
        first_with_images = None
        for model in models:
            sources_kwargs = dict(images=item.get("images", True), fill_details=True, add_tags=item.get("add_tags", False),
                                  linked_by="download", details=details)
            if first_with_images is not None:
                sources_kwargs["reuse_images_from"] = first_with_images
            from app.source_linking import link_model_to_listing   # lazy: that module pulls in the matching job
            link_model_to_listing(session, model, provider, source_id, **sources_kwargs)
            if first_with_images is None and item.get("images", True):
                first_with_images = model.id
        return [m.id for m in models]
    finally:
        shutil.rmtree(temp, ignore_errors=True)


# ---------- the queue ----------

_lock = threading.Lock()
_items: list = []
_worker_running = False
_cancel_ids: set = set()


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("images", "add_tags", "file_ids")}


def snapshot() -> dict:
    with _lock:
        items = [_public(i) for i in _items]
        counts = {}
        for i in items:
            counts[i["status"]] = counts.get(i["status"], 0) + 1
        return {"running": _worker_running, "items": items, "counts": counts}


def find_item(provider: str, source_id: str) -> Optional[dict]:
    with _lock:
        for item in reversed(_items):
            if item["provider"] == provider and item["source_id"] == str(source_id):
                return _public(item)
    return None


def _clean_file_ids(value) -> Optional[list]:
    if not isinstance(value, list) or not value:
        return None
    return [str(v)[:200] for v in value[:100]]


def enqueue(listings: list, images: bool = True, add_tags: bool = False) -> list:
    """Add listings [{provider, source_id, title?, thumbnail?, file_ids?}] to the queue.
    file_ids picks which of the listing's files to fetch (default: its model files).
    A listing that is already waiting or running is not added twice."""
    added = []
    with _lock:
        active = {(i["provider"], i["source_id"]) for i in _items if i["status"] in ("queued", "downloading", "importing")}
        for listing in listings:
            key = (listing["provider"], str(listing["source_id"]))
            if key in active:
                continue
            active.add(key)
            item = {
                "id": uuid.uuid4().hex[:12], "provider": key[0], "source_id": key[1],
                "title": (listing.get("title") or "")[:200], "thumbnail": listing.get("thumbnail"),
                "status": "queued", "message": "Waiting", "bytes_done": 0, "bytes_total": 0,
                "model_ids": [], "created_at": datetime.utcnow().isoformat(), "finished_at": None,
                "images": images, "add_tags": add_tags, "file_ids": _clean_file_ids(listing.get("file_ids")),
            }
            _items.append(item)
            added.append(item["id"])
        if len(_items) > MAX_KEPT_ITEMS:                      # forget the oldest finished ones
            finished = [i for i in _items if i["status"] not in ("queued", "downloading", "importing")]
            for old in finished[: len(_items) - MAX_KEPT_ITEMS]:
                _items.remove(old)
    if added:
        _ensure_worker()
    return added


def clear_finished() -> int:
    with _lock:
        before = len(_items)
        _items[:] = [i for i in _items if i["status"] in ("queued", "downloading", "importing")]
        return before - len(_items)


def cancel(item_id: str) -> bool:
    """A waiting item is dropped; a running one stops at its next chunk."""
    with _lock:
        for item in _items:
            if item["id"] != item_id:
                continue
            if item["status"] == "queued":
                item.update(status="cancelled", message="Cancelled", finished_at=datetime.utcnow().isoformat())
                return True
            if item["status"] in ("downloading", "importing"):
                _cancel_ids.add(item_id)
                return True
    return False


def _ensure_worker() -> None:
    global _worker_running
    with _lock:
        if _worker_running:
            return
        _worker_running = True
    threading.Thread(target=_worker, name="downloads", daemon=True).start()


def _next_queued() -> Optional[dict]:
    with _lock:
        for item in _items:
            if item["status"] == "queued":
                item["status"], item["message"] = "downloading", "Starting..."
                return item
    return None


def _update(item: dict, **values) -> None:
    with _lock:
        item.update(values)


def process_item(item: dict) -> None:
    """Run one queued item to completion (also called directly by tests)."""
    def progress(done, total):
        _update(item, bytes_done=done, bytes_total=total)

    def cancelled():
        return item["id"] in _cancel_ids

    def set_status(status, message):
        _update(item, status=status, message=message)

    finished = datetime.utcnow().isoformat()
    try:
        with Session(engine) as session:
            existing = session.exec(select(Model3D).where(
                Model3D.source_provider == item["provider"], Model3D.source_id == item["source_id"])).all()
            if existing:
                _update(item, status="skipped", message="Already in your library", model_ids=[m.id for m in existing],
                        finished_at=finished)
                return
            credentials = sources.load_credentials(session)
            ids = download_listing(session, item, credentials, progress, cancelled, set_status)
        _update(item, status="done", message=f"Added {len(ids)} model{'s' if len(ids) != 1 else ''} to the library",
                model_ids=ids, finished_at=datetime.utcnow().isoformat())
    except Cancelled:
        _update(item, status="cancelled", message="Cancelled", finished_at=finished)
    except sources.SourceError as e:
        _update(item, status="error", message=str(e), finished_at=finished)
    except Exception:
        logger.exception("Download of %s %s failed", item["provider"], item["source_id"])
        _update(item, status="error", message="Something went wrong; see the container log", finished_at=finished)
    finally:
        _cancel_ids.discard(item["id"])


def _worker() -> None:
    global _worker_running
    try:
        while True:
            item = _next_queued()
            if item is None:
                break
            process_item(item)
    finally:
        with _lock:
            _worker_running = False
        if _next_queued_exists():          # something was added just as the worker was finishing
            _ensure_worker()


def _next_queued_exists() -> bool:
    with _lock:
        return any(i["status"] == "queued" for i in _items)
