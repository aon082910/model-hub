# Model Hub (self-hosted)

[![tests](https://github.com/aon082910/model-hub/actions/workflows/tests.yml/badge.svg)](https://github.com/aon082910/model-hub/actions/workflows/tests.yml)

Source: [github.com/aon082910/model-hub](https://github.com/aon082910/model-hub) ·
Image: [hub.docker.com/r/allornothing/model-hub](https://hub.docker.com/r/allornothing/model-hub)

An open, self-hosted clone of [meshory.com](https://meshory.com)'s feature set for Unraid:
STL/3MF/OBJ/STEP/FBX library management, thumbnails, duplicate detection, AI
auto-tagging + semantic search (local Ollama **or** an external API — your
choice, switchable in Settings), smart collections, filament inventory, and a
print queue. Meshory itself is closed-source and desktop-only; this is a
from-scratch reimplementation of its feature list, not a repackage of Meshory.

## Run it locally / test before deploying to Unraid

```bash
docker compose up --build
```

Then open http://localhost:8420. Put some STL/3MF files in `./data`, click
**Rescan Library**.

## Deploy to Unraid

1. **Image is published** at [allornothing/model-hub](https://hub.docker.com/r/allornothing/model-hub)
   on Docker Hub — Unraid can pull it directly, no build step needed. (To build your own fork
   instead: `docker build -t <you>/model-hub:latest . && docker push <you>/model-hub:latest`,
   then edit `unraid/model-hub.xml` to match.)

2. **(Optional, for local AI) Install Ollama from Community Applications first** —
   search "ollama" in the Apps tab, install it, then in its container console run:
   ```bash
   ollama pull llava
   ollama pull nomic-embed-text
   ```

3. **Add the Model Hub template**: Docker tab → Add Container → toggle
   "Template" mode off → in the **Template** field near the top paste:
   `https://raw.githubusercontent.com/aon082910/model-hub/master/unraid/model-hub.xml`
   — Unraid fetches it and pre-fills everything below. (Alternative: copy
   `unraid/model-hub.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the
   flash drive and it'll appear under "User templates" instead.)

4. Set **Library Path** to the Unraid share holding your model files (e.g.
   `/mnt/user/models/`) and **App Config/DB** to an appdata folder. **PUID**/**PGID**
   default to 99/100 (Unraid's `nobody:users`) — the container runs as this uid on
   startup and chowns `/config` to it, so files it creates show up owned by a real
   host user instead of root. Apply.

5. Open the WebUI, go to **Settings**, pick AI mode:
   - **Local (Ollama)** — point "Host" at `http://<unraid-ip>:11434` (or the
     Ollama container's name if both are on the same custom Docker network).
     Nothing leaves your network.
   - **API** — paste an OpenRouter/OpenAI-compatible key. Same model Meshory
     itself uses today.
   - **Off** — pure manual tagging, no AI calls at all.

6. Click **Rescan Library**, then **Tag All (AI)** if you want auto-tagging.

7. First time you open the WebUI you'll land on a **Create admin account** screen
   (skipped if you set Admin Username/Password in step 4). This gates the whole
   app — see [Auth](#auth) below.

## What's implemented vs. Meshory's roadmap

| Feature | Status |
|---|---|
| STL/3MF/OBJ/FBX/STEP viewer & thumbnails | STL/OBJ/3MF/FBX/STEP/STP all wired to a live Three.js viewer |
| STEP/STP | Live-viewed via [occt-import-js](https://github.com/kovacsv/occt-import-js) (OpenCascade compiled to WASM, runs client-side, tessellates the B-rep into a mesh) — lazy-loaded on first STEP open so it doesn't cost anything for people who never open one |
| Duplicate detection (hash + geometry) | Done — SHA256 content hash + normalized-vertex geometry hash |
| Collections | Done |
| Smart/rule-based collections | Done — field/operator/value rules (`app/smart_collections.py`) |
| AI auto-tagging, local or API | Done — pluggable provider (`app/ai/`), pausable/resumable batch job, rough cost estimate for paid API mode |
| Semantic search | Done — embeddings stored per-model, cosine similarity search |
| Slicer hand-off | Implemented as network-path + direct-download hand-off (`app/routers/slicer.py`) — a server container cannot launch an app on your desktop, so this exposes the same share path your slicer can watch/import from, rather than faking a "send to slicer" button |
| Metadata/license/designer tracking | Done — fields on each model, editable in the viewer |
| Filament inventory | Done — CRUD + automatic consumption tracking (deducted when a print queue item is marked "done", see [Print estimates](#print-time--filament-estimates)) |
| Print queue | Done — ordered queue with status, filament assignment, and estimated grams/time per job |
| Browser extension for Printables/MakerWorld import | Done — `browser-extension/` (Manifest V3), see [Browser extension](#browser-extension) below |
| Login/auth | Done — see [Auth](#auth) below |
| Print time / filament weight estimate | Done — see [Print estimates](#print-time--filament-estimates) below |
| Notifications | Done — see [Notifications](#notifications) below |

## Auth

A single admin account gates the entire app and API (except `/api/health` and the
login/setup endpoints themselves). Session is a signed, HttpOnly cookie, 30-day expiry;
password is PBKDF2-SHA256 hashed (200k iterations), never stored or returned in plaintext.
`/api/auth/login` is rate-limited per client IP (5 failed attempts / 15 minutes, in-memory)
to slow down brute-force guessing; a locked-out client gets a `429` with `Retry-After`.

- **First run**: the WebUI shows a **Create admin account** screen. Or set `AUTH_USERNAME`
  + `AUTH_PASSWORD` container env vars to skip it (the account is created from those on
  first startup only — changing them later does nothing once an account exists).
- **Change password**: Settings → Account.
- **The browser extension does *not* use this login.** It authenticates with a separate,
  narrower **extension API key** (Settings → Browser Extension → copy the key into the
  extension popup). That key only unlocks `/api/library/import` — nothing else, so a
  leaked/synced extension key can't read your settings, change your password, or touch
  the rest of the library. Regenerate it any time from the same Settings panel.

## Print time / filament estimates

Every mesh's volume is computed at scan time (`app/thumbnails.py` → `mesh_stats`, uses the
real mesh volume for watertight meshes, the convex hull as an approximation otherwise).
From the viewer, pick a material + infill % and click **Estimate Print** to get a grams/
minutes estimate, or **Add to Print Queue** to attach that estimate to a queue entry.

Two estimate sources:
- **Heuristic (default, no setup)** — a volumetric approximation (shell + infill volume ×
  material density, flow-rate-based time). Clearly labeled `"source": "heuristic"` and
  typically within ~30-50% of a real slice for simple shapes — good enough for filament
  budgeting, not for scheduling a print queue to the minute.
- **Exact (opt-in)** — set `SLICER_CLI_PATH` to a headless slicer CLI binary bind-mounted
  into the container (e.g. a PrusaSlicer/OrcaSlicer AppImage extracted with
  `--appimage-extract`, since AppImages need FUSE the container doesn't have). When set,
  the model is actually sliced and `estimated_grams`/`estimated_minutes` come straight out
  of the generated G-code header. Response is then labeled `"source": "slicer"`.

When a print queue item's status is set to **done** and it has a filament assigned, its
`estimated_grams` is deducted from that spool's `remaining_g` exactly once.

## Notifications

Settings → Notifications → a webhook URL, fired (best-effort, failures are logged and
never block the underlying job) when a background scan finds new files or a tagging job
finishes. There's no way for a container to reach into the Unraid host and call its native
notify script, so this is a generic JSON POST instead — point it at:
- [ntfy.sh](https://ntfy.sh) (free, has an Unraid Community Apps entry for push notifications), or
- a Discord/Slack incoming webhook URL, or
- an Unraid User Script configured with a webhook trigger.

## Browser extension

`browser-extension/` is a separate, small Chrome/Edge (Manifest V3) extension. It is not
part of the Docker image — it installs in your browser and talks to your running Model Hub
server over the network.

**Install (unpacked, until it's published to a store):**
1. Open `chrome://extensions`, enable **Developer mode**.
2. **Load unpacked** → select the `browser-extension/` folder.
3. In Model Hub, go to **Settings → Browser Extension** and copy the API key.
4. Click the extension icon → enter your Model Hub server URL (e.g. `http://192.168.1.50:8420` —
   your Unraid host's IP and the WebUI port) and paste the API key → **Save** (grants the
   extension permission to reach that one origin) → **Test Connection**.

**Use:** open a model page on printables.com or makerworld.com. A **📦 Send to Model Hub**
button appears bottom-right. It reads the page's `schema.org` JSON-LD (title/author/license —
the same structured data search engines use, which is far less brittle than scraping CSS
classes) and scans the page for direct `.stl/.3mf/.step/.obj/.fbx/.zip` links, lets you
pick which files and edit designer/license, then downloads each file and POSTs it to
`/api/library/import` on your server, which files it under `imported/` in your library and
tags it with the source URL/designer/license automatically.

Caveat: Printables sits behind a Cloudflare bot-check on some requests/regions, and both
sites can change their markup. The extension deliberately avoids hardcoded CSS selectors
(hence the JSON-LD approach) to stay resilient, but if a site's download links are themselves
gated behind JS or auth you'll need to open the direct file URL in a tab first.

## Architecture

- **Backend**: FastAPI + SQLModel (SQLite) — `app/main.py`, `app/routers/*`
- **Scanning**: `app/scanner.py` walks the mounted library, hashes files, detects duplicates
- **Thumbnails**: `app/thumbnails.py` via trimesh (headless render, matplotlib fallback); also computes volume/watertightness for print estimates
- **Viewer**: `app/static/app.js` + Three.js for STL/OBJ/3MF/FBX; STEP/STP tessellated client-side by `occt-import-js` (WASM OpenCascade) into a Three.js mesh
- **AI**: `app/ai/` — `OllamaProvider` (local) and `APIProvider` (OpenAI/OpenRouter-compatible), selected per the `ai_mode` setting
- **Auth**: `app/auth.py` + `app/routers/auth_router.py` — session cookie for the WebUI, a separate narrower-scoped API key for the browser extension, enforced by a single ASGI middleware in `main.py`
- **Print estimates**: `app/estimate.py` — volumetric heuristic by default, or exact numbers via an optional external slicer CLI (`SLICER_CLI_PATH`)
- **Notifications**: `app/notify.py` — generic webhook POST, best-effort
- **Migrations**: `app/db.py` auto-adds new columns to existing SQLite tables on startup (no Alembic; fine for this project's size, but note it if you fork it)
- **Frontend**: vanilla JS + Three.js, no build step (`app/static/`) — mobile-responsive down to phone widths (scrollable tab bar, stacked toolbars/forms, full-screen viewer modal)

## CI / Tests

`tests/test_app.py` is an end-to-end integration suite against a real (temp-dir) instance
of the app — auth setup/login/logout, the extension API key's scope (import-only), mesh
volume parsing on a real generated STL, duplicate detection, print estimates, and the
filament-deduction-happens-exactly-once behavior. Run locally:

```bash
pip install -r requirements-dev.txt
pytest
```

Two workflows:
- **`.github/workflows/tests.yml`** — runs the pytest suite plus a no-push Docker build
  (build-health check only) on every push to `master` and every PR. No secrets needed.
- **`.github/workflows/release.yml`** — only on pushing a `v*` tag (or manual dispatch with
  a version input) — builds and pushes `allornothing/model-hub:latest` and `:<version>` to
  Docker Hub, then creates a GitHub release. Needs one repository secret that isn't set by
  default — add it under **Settings → Secrets and variables → Actions**:
  - `DOCKERHUB_TOKEN` — a Docker Hub [access token](https://hub.docker.com/settings/security)
    with Read & Write permission (not your password). The username is `allornothing`, baked
    into the workflow — no separate username secret needed.

  To cut a release: `git tag v1.0.1 && git push --tags`, or run the workflow manually from
  the Actions tab with a version input.
