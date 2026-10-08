import asyncio
import hashlib
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.db import init_db, engine
from app.config import SCAN_INTERVAL_SECONDS
from app.routers import library, tags, collections, filament, inventory, projects, sources, source_match, discover, wishlist, backup, prints, duplicates, queue, settings, ai, slicer, auth_router
from app.auth import path_requires_auth, request_is_authenticated, bootstrap_from_env, ensure_extension_api_key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("modelhub")

app = FastAPI(title="Model Hub")

# Permissive CORS: this app is meant to run on a private LAN/Unraid host, and the
# Model Hub browser extension (running as an extension background worker, on an
# origin the admin doesn't control) needs to POST imports to it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(library.router)
app.include_router(tags.router)
app.include_router(collections.router)
app.include_router(filament.router)
app.include_router(projects.router)
app.include_router(inventory.router)
app.include_router(sources.router)
app.include_router(source_match.router)
app.include_router(discover.router)
app.include_router(wishlist.router)
app.include_router(backup.router)
app.include_router(prints.router)
app.include_router(duplicates.router)
app.include_router(queue.router)
app.include_router(settings.router)
app.include_router(ai.router)
app.include_router(slicer.router)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    # let CORS preflights through unauthenticated -- the browser never attaches
    # cookies/headers to an OPTIONS preflight, so gating it here would just
    # break cross-origin POSTs (the extension) before CORS gets to answer them
    if request.method != "OPTIONS" and path_requires_auth(request.url.path):
        from sqlmodel import Session
        with Session(engine) as session:
            if not request_is_authenticated(request, session):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)

STATIC_DIR = Path(__file__).parent / "static"

# The app's own scripts/styles (not vendor/, which only changes with a rewrite)
# get a ?v=<content hash> in index.html, so an upgrade changes their URLs and
# browsers can't keep running the previous release's JavaScript.
VERSIONED_ASSETS = ("style.css", "library-controls.js", "thumbnail-controls.js", "app.js")


def _asset_version() -> str:
    digest = hashlib.sha1()
    for name in VERSIONED_ASSETS:
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:10]


def _render_index() -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    version = _asset_version()
    for name in VERSIONED_ASSETS:
        html = html.replace(f'"/assets/{name}"', f'"/assets/{name}?v={version}"')
    return html


INDEX_HTML = _render_index()


class RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers must revalidate (ETag -> cheap 304) before
    reuse, so unversioned URLs such as vendor/ can't go stale either."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/assets", RevalidatingStaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
def index():
    # no-cache: the page that carries the version tokens must itself never be stale
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    return {"status": "ok"}


def _run_scan():
    """Run one library scan outside the asyncio event loop."""
    from sqlmodel import Session
    from app.scanner import scan_library
    from app.notify import notify

    with Session(engine) as session:
        result = scan_library(session)
        if result.get("added"):
            notify(session, "Model Hub: new files", f"{result['added']} new model(s) added to your library.")
        return result


async def _background_scan_loop():
    from app.scanner import ScanAlreadyRunning

    while True:
        try:
            # scan_library performs blocking filesystem, hashing, mesh parsing,
            # thumbnail rendering, and SQLite work. Run it in a worker thread so
            # Uvicorn can finish startup and continue servicing HTTP requests.
            result = await asyncio.to_thread(_run_scan)
            logger.info("Background scan: %s", result)
        except ScanAlreadyRunning:
            logger.info("Background scan skipped: library scan already in progress")
        except Exception:
            logger.exception("Background scan failed")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup():
    init_db()
    from sqlmodel import Session
    with Session(engine) as session:
        bootstrap_from_env(session)
        ensure_extension_api_key(session)
    asyncio.create_task(_background_scan_loop())


@app.on_event("shutdown")
async def on_shutdown():
    from app.mesh_worker import shutdown_worker_pools
    shutdown_worker_pools()
