import asyncio
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.db import init_db, engine
from app.config import SCAN_INTERVAL_SECONDS
from app.routers import library, tags, collections, filament, inventory, projects, sources, source_match, discover, wishlist, backup, prints, duplicates, designers, source_updates, users, printers, bulk, saved_searches, families, shares, labels, system, stats, tokens, print_files, activity, library_io, filing_rules, estimates, planner, slots, maintenance, slicer_links, fit, offsite_router, costs, orders, spoolman_router, share_target, creators, storage_router, queue, settings, ai, slicer, auth_router
from app.auth import path_requires_auth, current_user, forbidden_reason, bootstrap_from_env, ensure_extension_api_key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("modelhub")

@asynccontextmanager
async def lifespan(app):
    init_db()
    from sqlmodel import Session
    with Session(engine) as session:
        bootstrap_from_env(session)
        ensure_extension_api_key(session)
    tasks = [asyncio.create_task(_background_scan_loop())]
    if not os.environ.get("MODELHUB_DISABLE_SCHEDULER"):          # (tests switch the timers off)
        from app import scheduler
        tasks.append(asyncio.create_task(scheduler.loop()))
    yield
    for task in tasks:
        task.cancel()
    from app.mesh_worker import shutdown_worker_pools
    shutdown_worker_pools()


app = FastAPI(title="Model Hub", lifespan=lifespan)

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
app.include_router(designers.router)
app.include_router(source_updates.router)
app.include_router(users.router)
app.include_router(printers.router)
app.include_router(bulk.router)
app.include_router(saved_searches.router)
app.include_router(families.router)
app.include_router(shares.router)
app.include_router(shares.public_router)
app.include_router(labels.router)
app.include_router(system.router)
app.include_router(stats.router)
app.include_router(tokens.router)
app.include_router(print_files.router)
app.include_router(activity.router)
app.include_router(library_io.router)
app.include_router(filing_rules.router)
app.include_router(estimates.router)
app.include_router(planner.router)
app.include_router(slots.router)
app.include_router(maintenance.router)
app.include_router(slicer_links.router)
app.include_router(fit.router)
app.include_router(offsite_router.router)
app.include_router(costs.router)
app.include_router(share_target.router)
app.include_router(creators.router)
app.include_router(storage_router.router)
app.include_router(orders.router)
app.include_router(spoolman_router.router)
app.include_router(slicer_links.public_router)
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
            user = current_user(request, session)
        if user is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        reason = forbidden_reason(user, request.method, request.url.path)
        if reason:
            return JSONResponse({"detail": reason}, status_code=403)
        request.state.user = user
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


@app.get("/manifest.webmanifest")
def web_manifest():
    return FileResponse(STATIC_DIR / "pwa" / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    # served from the root so it may control the whole app; never cached, so an upgrade replaces it
    return FileResponse(STATIC_DIR / "pwa" / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/api/health")
def health():
    return {"status": "ok"}


def _run_scan():
    """Run one library scan outside the asyncio event loop."""
    from sqlmodel import Session
    from app.scanner import scan_library
    from app.notify import notify_event

    with Session(engine) as session:
        result = scan_library(session)
        if result.get("added"):
            notify_event(session, "new_files", "Model Hub: new files", f"{result['added']} new model(s) added to your library.")
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
