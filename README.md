# Model Hub (self-hosted)

[![tests](https://github.com/aon082910/model-hub/actions/workflows/tests.yml/badge.svg)](https://github.com/aon082910/model-hub/actions/workflows/tests.yml)

Source: [github.com/aon082910/model-hub](https://github.com/aon082910/model-hub) ·
Image: [hub.docker.com/r/allornothing/model-hub](https://hub.docker.com/r/allornothing/model-hub)

An open, self-hosted 3D-print-file-library manager for Unraid:
STL/3MF/OBJ/STEP/FBX library management, thumbnails, duplicate detection, AI
auto-tagging + semantic search (local Ollama **or** an external API — your
choice, switchable in Settings), smart collections, filament inventory, and a
print queue.

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
   - **API** — paste an OpenRouter/OpenAI-compatible key.
   - **Off** — pure manual tagging, no AI calls at all.

6. Click **Rescan Library**, then **Tag All (AI)** if you want auto-tagging.

7. First time you open the WebUI you'll land on a **Create admin account** screen
   (skipped if you set Admin Username/Password in step 4). This gates the whole
   app — see [Auth](#auth) below.

## Feature status

| Feature | Status |
|---|---|
| STL/3MF/OBJ/FBX/STEP viewer & thumbnails | STL/OBJ/3MF/FBX/STEP/STP all wired to a live Three.js viewer |
| STEP/STP | Live-viewed via [occt-import-js](https://github.com/kovacsv/occt-import-js) (OpenCascade compiled to WASM, runs client-side, tessellates the B-rep into a mesh) — lazy-loaded on first STEP open so it doesn't cost anything for people who never open one |
| Duplicate detection (hash + geometry) | Done — SHA256 content hash + normalized-vertex geometry hash |
| Collections | Done |
| Smart/rule-based collections | Done — field/operator/value rules (`app/smart_collections.py`) |
| AI auto-tagging, local or API | Done — pluggable provider (`app/ai/`), pausable/resumable batch job, rough cost estimate for paid API mode. Batch runs checkpoint (session release + GC) every 20 models, provider responses are streamed with a hard size cap so one pathological response can't OOM the container, and the job checks the container's own cgroup memory usage before each model and stops itself cleanly if it's running critically high |
| Semantic search | Done — embeddings stored per-model, cosine similarity search |
| Slicer hand-off | Implemented as network-path + direct-download hand-off (`app/routers/slicer.py`) — a server container cannot launch an app on your desktop, so this exposes the same share path your slicer can watch/import from, rather than faking a "send to slicer" button |
| Metadata/license/designer tracking | Done — fields on each model, editable on the model's page |
| Filament inventory | Done — CRUD + automatic consumption tracking (deducted when a print queue item is marked "done", see [Print estimates](#print-time--filament-estimates)) |
| Print queue | Done — ordered queue with status, filament assignment, and estimated grams/time per job |
| Projects & parts tracking | Done — group library models into a project and keep its parts list (electronics / parts / supplies) with quantity needed vs. owned, unit cost and a purchase link. The **Projects** tab can filter to projects still needing parts, and a **Shopping List** rolls up every missing part across unfinished projects with an estimated total. Add a model to a project from its viewer. Each model in a project can be given one or more filament spools with grams per spool (the **Estimate** button fills grams from the print estimate); when the project is set to **printed** (or **done**) those grams are subtracted from filament inventory exactly once, and moving the project back to planning/building restores them |
| Shopping list export | Done — the Projects tab's **Shopping List** can be downloaded as CSV or plain text, or copied to the clipboard (for a notes app or your phone). **Combine identical parts across projects** merges the same part from different projects into one line; each line also shows how many you already have in Supplies |
| Supplies on hand | Done — a **Supplies** tab for the electronics, parts and supplies you own: quantity, a low-stock threshold (flagged and filterable), location (bin/drawer), unit cost, search, and CSV export. Typing a project part name suggests items from here and pre-fills its type and cost |
| Match models to online listings | Done — Printables, MakerWorld and Sketchfab (no account) plus Thingiverse, MyMiniFactory and Cults3D (keys entered in Settings); see [Matching models to site listings](#matching-models-to-site-listings) below |
| Search and add to library | Done — a **Search** page searches your library and every connected site at once; open a result for a full preview page, add one with a button, or tick several and add them together. See [Search and add to library](#search-and-add-to-library) |
| Backup and restore | Done — **Settings → Backup** downloads (or saves on the server) a zip with everything Model Hub knows apart from the model files, and restores from one. See [Backup and restore](#backup-and-restore) |
| Print log | Done — every model's page has a print history (date, rating, filament and grams, minutes, notes, a photo); the Library can filter to printed or never-printed models and shows a printed badge; finishing a job in the print queue logs it automatically. See [Print log](#print-log) |
| Duplicate cleanup | Done — **Library → Review duplicates** lists groups of identical files, keeps the one you pick (carrying tags, collections, projects, notes, print history and listing link over), and can delete the other files after checking them again. See [Duplicates](#duplicates) |
| Follow designers | Done — **Follow** a designer from one of their listings; the **Following** tab shows their new uploads (Printables and Sketchfab need no account, Thingiverse uses your token). See [Following designers](#following-designers) |
| Listing updates | Done — **Matches → Listing updates** checks the online listing of every linked model and flags changed descriptions, pictures, tags, licenses and files; refresh the model from the listing or dismiss the flag. See [Listing updates](#listing-updates) |
| Project costs | Done — give each spool a price (Filament tab) and a project shows what it costs: filament, every part (owned or not), and optionally electricity (price per kWh and printer watts in Settings); it is in the project PDF too. See [Project costs](#project-costs) |
| More than one login | Done — the first account is the administrator; **Settings → People** adds *members* (everything except Settings, backups, user management and deleting duplicate files) and read-only *viewers*. See [People](#people) |
| Install on a phone | Done — Model Hub is an installable web app (manifest, icons, a service worker that caches nothing); pages are checked for horizontal overflow at phone width. See [On your phone](#on-your-phone) |
| Printers | Done — add Klipper/Moonraker and OctoPrint printers, see their status, send them a G-code file, or slice a model first when a slicer is configured. Never starts a print unless you ask. See [Printers](#printers) |
| Scheduled jobs and notifications | Done — a weekly (or daily, or off) saved backup, a check for new uploads from followed designers, low filament and low supply warnings, an optional daily check of linked listings, and noticing when a printer finishes a print; each kind of notification can be switched off. See [Things Model Hub does by itself](#things-model-hub-does-by-itself) |
| Library filters, sorting, saved searches | Done — **Library → Filters** adds designer, license, collection, project, linked-or-not, has-notes and **Fits my printer's bed** filters, six sort orders, and saved searches. See [Library tools](#library-tools) |
| Bulk edit | Done — **Select** in the Library, pick models (this page, or everything that matches the filters), then add or remove a tag, collection or project, set the designer or license, or add them to the print queue |
| Print settings and print again | Done — every model's page has **What worked** (material, layer height, infill, supports, temperatures, speed, slicer profile, notes) and **Print again**, which queues it with the filament, grams and time of its last print |
| Filament prices and low spools | Done — a spool's price history is kept (Filament tab, **prices**), and spools at or below the low-filament level appear on the shopping list (and its CSV and text exports) with their link and last price |
| Versions of a model | Done — group files as versions (v1, v2...), label them, see them on each model's page, and hide older versions with **Latest versions only**; suggestions come from similar names and identical shapes. See [Versions, sharing and labels](#versions-sharing-and-labels) |
| Share links | Done — a secret read-only link to a model or a project for someone without a login; downloads and part costs are off unless you switch them on, notes and print history are never shown, links can expire or be revoked |
| QR labels | Done — printable QR labels for filament spools and supplies; scanning one with a phone camera opens that item |
| 3D view tools | Done — **Measure** (click two points), **Section** (cut with a plane to look inside) and **Compare** with another version (overlaid in orange, with the size difference) |
| Statistics | Done — the **Stats** tab: prints, models printed, filament used, print time and filament cost per month (charts for the last 6, 12 or 24 months), most printed models, filament by material, printer success rate (from the finished and cancelled prints printers report), and how much of the library has never been printed |
| Update notice | Done — Model Hub looks for a newer release once a day (switch off in Settings → Updates; only the public GitHub release information is requested) and shows an *Update* badge to the administrator, plus a notification once per new version |
| API tokens | Done — **Settings → API tokens**: read, write and import keys for scripts and other programs; shown once, stored only as a hash. See [API tokens](#api-tokens) |
| Wishlist | Done — a **Wishlist** tab keeps listings you want for later (save from a search result or a listing page, with a note), shows which are already in your library, and **Add all to library** queues every one the server can download in one go; the rest are reported with the reason. See [Wishlist](#wishlist) |
| Choose which files to download | Done — a listing's page lists every file (Printables' individual STLs as well as its model pack, Thingiverse's files, ...); tick the ones you want, or leave the default (the pack / all model files) |
| Wikimedia Commons and NASA 3D Resources | Done — two more sites that need no account and let the server download: Commons' 3D files and NASA's public-domain 3D Resources (Apollo landing sites, satellites, ...). Both are searchable and downloadable from the Search tab; they are left out of the whole-library match job, since a personal file is unlikely to come from there |
| Bulk unlinking | Done — **Matches → Linked models** lists every linked model (filter by site and how it was linked) and unlinks the ticked ones, or everything matching the filters, in one go |
| Match your whole library | Done — the **Matches** tab runs a background job that searches Printables and MakerWorld for every model that isn't linked yet and keeps the closest listings as a review queue; you link, tick-and-link in bulk, or skip. See [Matching models to site listings](#matching-models-to-site-listings) |
| Import a listing's parts into a project | Done — paste a MakerWorld link (or use **Import parts** on a model linked to one) to get the listing's store items and parts list as editable rows, then add the ticked ones to the project in one go |
| Model pages | Done — every model has its own page (`#/model/<id>`): a big 3D view, details (size, dimensions, volume, watertight), editable tags, designer / license / notes, the listing it is linked to, the projects and collections it is in, print estimate and queue, and any copies of the same file |
| Project pages and PDF export | Done — every project has its own page (`#/project/<id>`) with editable name, description and notes, its models (with pictures and filament), and its parts list. **Export PDF** makes a one-file summary: description, notes, models with pictures, designer / license / listing links, filament and a parts list with what is still to buy |
| Model files only | Done — the library indexes and imports only model files (.stl .3mf .obj .step .stp .fbx). Zips, notes and other files in the library folder are ignored; importing a .zip extracts just the models inside. Settings has a cleanup for older entries |
| Browser extension for Printables/MakerWorld import | Done — `browser-extension/` (Manifest V3), see [Browser extension](#browser-extension) below |
| Login/auth | Done — see [Auth](#auth) below |
| Print time / filament weight estimate | Done — see [Print estimates](#print-time--filament-estimates) below |
| Notifications | Done — see [Notifications](#notifications) below |

## Matching models to site listings

Open a model and use **Find on MakerWorld / Printables** (or paste a model link). Model Hub searches both
sites using the file name (`Benchy_v2-fixed.stl` becomes "Benchy"), lists the matches best first with a
match percentage, and **Link** pulls in that listing's title, designer, license, description, tags and
pictures. Options let you skip the pictures, leave designer/license alone, or add the site's tags as
Model Hub tags. **Re-fetch from site** refreshes a link and **Unlink** removes it and the saved pictures.
Models without a rendered thumbnail (STEP, FBX...) show the listing's first picture in the library grid.

Models imported through the [browser extension](#browser-extension) from a Printables or MakerWorld page
are matched automatically from the page address, so they arrive with their details filled in.

- **Sites**: Printables is read through its public GraphQL API; MakerWorld through the JSON API behind
  `api.bambulab.com` (makerworld.com itself blocks server-side page requests); Sketchfab through its public search
  API (only downloadable models are searched). These three need no account. **Thingiverse** (access token),
  **MyMiniFactory** (API key) and **Cults3D** (your nickname plus an API key) are searched once you paste their
  credentials into **Settings → Model Sites** (each has a **Test** button, so you can configure them whenever you
  have the keys). Credentials stay on the server and are never shown again; Thingiverse's token travels in a request
  header, and MyMiniFactory's key (which that site only accepts in the URL) is kept out of the log. The first three
  APIs are unofficial or public-but-undocumented, so a failure is reported in the UI instead of breaking anything, and
  one site being down never hides the other's results.
- **Not included**: Thangs (its API is behind a bot challenge) and the NIH 3D Print Exchange and Creality Cloud
  (no usable public API).
- **Safe by construction**: only those sites' API and image hosts are contacted, so a pasted link can't make the
  server fetch anything else on your network.
- **Pictures are shrunk** to preview-sized JPEGs (at most 1200 px, up to 6 per model, stored under
  `/config/source_images/`), and oversized images are refused before they are decoded.
- Matching is by name, so check the match before linking; a wrong link is one click to undo.

### Match your whole library

The **Matches** tab does the lookup for every model that isn't linked yet. **Find matches** starts a background
job that searches both sites by file name and keeps the best few listings per model (name similarity of 45% or
better) as candidates. Nothing is linked by the job itself: you review the queue, best matches first.

- **Link** one candidate, or tick rows (**Tick all 90%+** helps) and **Link ticked** to take each row's best
  match. The pictures / designer-license / tags options above the queue apply to every link you make.
- **Link clear winners automatically** (off by default) links, without asking, any model whose best match scores
  at least 90 / 95 / 98% *and* is at least 10 points ahead of the runner-up, so look-alike copies of a popular model
  are still left for you. Those models are listed under **Linked automatically** for a second look
  (**Looks right** or **Unlink**), and carry an *auto-linked* badge on their page.
- **None of these** marks a model as skipped so it stays out of the queue and future runs.
- The job goes easy on the sites (about one search pair per second), stops by itself if a site rate limits or
  keeps failing, and can be stopped any time. It remembers what it has checked, so running it again only looks
  at new models; tick **Also search again for models that had no match** to retry those too.
- Models whose names say nothing (a hash like `a1b2c3d4e5f6.stl`) are not searched.

### Parts lists from MakerWorld

MakerWorld listings can include what you need to buy besides the print. In a project, **Import parts from a
MakerWorld listing** (or the **Import parts** button on a model that is linked to a MakerWorld listing) fetches that
list: Bambu Lab store items (with link and price) and the designer's own parts list (servos, boards, screws...).
Each row comes with a guessed type (electronics / parts / supplies) and can be edited before adding; parts already
in the project are flagged and unticked. Filament entries are skipped, since filament is tracked separately.
A store item's quantity is how the listing counts it, not necessarily packs to buy, so check pack sizes in the name.
Only MakerWorld listings have a machine-readable parts list.

### Filament suggestions

A MakerWorld listing also says which filament it was designed for (e.g. PLA Basic, Gray). On a project page, a model
linked to such a listing shows **The listing suggests:** with each suggestion matched to one of your own spools (same
material, same colour name; "grey" and "gray" count as the same). **Use <spool>** adds a filament line for that spool,
with the grams filled from the print estimate. Suggestions with no matching spool say so.

## Search and add to library

The **Search** tab searches your library (file names, tags, descriptions, designer, listing titles and your notes) and every
site you have enabled, at once. Tick the sites to search (sites that still need a key are greyed out), and use
**Load more results** to page through all of them together.

- **Preview**: click a result for its own page (`#/listing/<site>/<id>`): pictures, designer, license, the full description,
  tags, likes, the files that would be downloaded, MakerWorld's parts list and suggested filament, and **Open on <site>**.
- **Add to library**: downloads the listing's model files into `imported/<Site>/<title> [<id>]/`, keeps only model file types
  (a pack zip is opened and just the models are kept), and links every model to the listing, so it arrives with its pictures,
  designer, license and (optionally) tags. A listing already in your library is never downloaded twice. Downloads run one at
  a time in the background with a progress list you can cancel from.
- **Bulk add**: tick any number of results (or **Tick all that can be added**) and **Add ticked to library**.
- **Choose files**: on a listing's page the *Files* box lists every file the server could download, with the default choice
  already ticked (Printables' model pack, or all of a Thingiverse listing's model files). Tick or untick files, or use
  **Tick all / Untick all**, before pressing **Add to library**. Files that are not model files are shown greyed out.
- **Save**: the **Save** button on a result or listing page puts it on the [Wishlist](#wishlist).

**Which sites let a server download files:** Printables (no login needed), Thingiverse (with your token), Wikimedia Commons and
NASA 3D Resources (both open, no account). MakerWorld,
Sketchfab and MyMiniFactory only give files to a logged-in user (MyMiniFactory's API key alone can't download), and Cults3D's
API never serves files at all. For those, the page says so and points to the **browser extension**, which downloads in
your own logged-in browser. Downloads are restricted to each site's own domains (every redirect is checked), the token is only
sent to Thingiverse's own hosts, and a single file is capped at 1.5 GB. Sites are searched in parallel, so one slow site
delays the results by its own time rather than adding up.

**Other sites looked at:** *Wikimedia Commons* and *NASA 3D Resources* were added because they have open interfaces that I
tested against the live sites. I could not find a public API for *NIH 3D*, *Thangs*, *Creality Cloud* or *Smithsonian 3D*
(their listings are only on their websites), so they were not added rather than guessing at scraping; if one of them
publishes an API later it is a small addition. For any site that needs a login to download, use the browser extension.

## Wishlist

The **Wishlist** tab keeps listings you want to come back to. Use **Save** on a search result or a listing page; each item can
carry a note. The tab shows which items are already in your library, lets you add one with a button, and **Add all to library**
queues every item the server can download (those already in your library are skipped; sites that need the browser extension
are reported with the reason). **Remove the ones already in my library** tidies the list afterwards. Downloads from the
wishlist use the same background queue as the Search page.

## Backup and restore

**Settings → Backup** has three things:

- **Download a backup** makes a zip of everything Model Hub knows apart from the model files: tags, collections, projects, supplies,
  filament, links to listings, notes, the wishlist, the print log, and the saved listing and print pictures. It is a consistent
  copy even while Model Hub is running. Passwords, the extension key and every site key or token are left out, so a backup is
  safe to keep around and restoring never replaces the logins you have now. Thumbnails are left out too (rebuild them from
  Settings if you restore onto a fresh install).
- **Save a copy on the server** keeps the same zip in `/config/backups`, which an Unraid appdata backup already covers.
- **Restore** from a downloaded file or a saved copy replaces the data tables in one transaction (so a bad file changes nothing),
  merges the non-secret settings, and first saves a copy of the current data as `before-restore-*.zip` (the newest three are kept), so
  a restore can be undone from the same list. Restores are refused while downloads or the matching job are running, and the file
  is checked first: it must be a Model Hub backup, nothing may be outside the expected folders, and a backup from a newer
  version is refused. Models in the backup whose file is not in this library folder are dropped by the next scan; the restore tells
  you how many.

## Print log

Every model's page has **Print history** and **Log a print**: date, rating (1–5), notes, optionally a spool and grams (which are taken
from the spool, never below zero; deleting the entry puts back exactly what it took), minutes, and a photo (shrunk like listing
pictures). Finishing a job in the **Print Queue** writes the entry for you, once per queue item. The Library can filter to **Printed** or
**Never printed**, and printed models show a badge with how many times. Print entries and photos go in backups, and are removed with the
model.

## Duplicates

Files are duplicates when their contents are identical (same hash). **Library → Review duplicates** shows each group with what is attached
to every copy (tags, collections, projects, prints, listing link, notes) and suggests the copy with the most of your own work on it.

- **Combine info** shares the tags, collections, projects, notes, designer/license and listing link with the kept copy, and leaves every file alone.
- **Keep this, delete the others** does that and also deletes the other files from your library folder and their records. Before any
  file is deleted both files are hashed again; a file that has changed since the scan, lies outside the library folder, or is the file
  you are keeping is never deleted, and nothing is deleted if the file you keep is missing. Queue entries, print history and project
  filament lines move to the kept model.
- **Clean up every group** does the suggested clean-up for all groups at once (with a confirmation).
- *Same shape, different file* lists files with the same geometry but different contents (re-exports and the like); these are only shown, never merged.

## Following designers

On a listing's page, **Follow <designer>** adds them to the **Following** tab. The first look only records what they have already uploaded;
after that, **Check for new uploads** (and a quiet check whenever you open the tab and it has been six hours) lists anything new, which you
can preview, **Save** to the wishlist, **Add to library**, or mark **Seen**. The tab's button shows how many are new.

| Site | Needs | Status |
|---|---|---|
| Printables | nothing | tested against the live site |
| Sketchfab | nothing | tested against the live site |
| Thingiverse | your token (Settings) | follows Thingiverse's documented `/users/<name>/things`; tested against a simulated server only |
| MakerWorld, MyMiniFactory, Cults3D | — | not available (no usable public list of a designer's models) |

## Listing updates

**Matches → Listing updates → Check for changes** runs a gentle background job (one listing at a time, stops if a site rate limits) over every linked
model. A listing shared by several models (a pack) is looked at once. The first check records a fingerprint of the listing's title,
description, tags, pictures, license and (for Printables, Thingiverse, Commons and NASA) file list; later checks flag what differs. **Refresh from the listing** updates
the model's saved title, description, tags and pictures (your own designer, license and notes are not touched) and starts a new baseline; **Dismiss** just accepts the change.
Listings checked in the last 20 hours are skipped.

## Project costs

Give a spool a **price** on the Filament tab (the price per kg is shown). A project then shows **Cost**: filament (grams per spool × that spool's price per gram; grams on
spools without a price are listed, not guessed), parts (quantity × unit cost for all of them, including ones you already own; the
existing "to buy" figure is unchanged) and, when **Settings → Electricity price per kWh** is set, electricity (printer watts, default 150 W,
× print hours). Print hours are worked out from the grams on the same assumptions as the volumetric print estimate, so treat electricity as a rough figure. The cost is in the project's PDF.

## Wishlist

Each item has a **priority** (high items sort first and are downloaded first) and a **status** (*Still want it*, *Got it elsewhere*, *Not any more*; only
the first is included in **Add all to library**). **Export as CSV** downloads the list. **Import links** takes any text with links (up to 40 at a time) and
adds each recognised listing; **Import my likes** and **Import this collection** pull things from Thingiverse with your token (these follow
Thingiverse's documented API and are tested against a simulated server only). Printables likes and collections need a login and can't be read from here.

## People

The login you create on first start is the **administrator**. **Settings → People** adds more logins:

| Role | Can |
|---|---|
| administrator | everything |
| member | use the library, projects, supplies, wishlist, following, matching, print log and so on; **not** Settings, backups, user management, printers, or deleting duplicate files |
| viewer | look at everything above, change nothing |

Roles are checked on every request, so changing a role or removing a person takes effect at once (a removed person's open session stops working). Members and viewers
can change their own password (the **Password** button next to their name). Logins are not part of a backup and a restore never replaces them.
The browser extension's API key is unchanged: it only opens the import door.

## On your phone

Open Model Hub in your phone's browser and use **Add to Home screen** (Android: *Install app*). It then opens like an app. Nothing from the app itself is cached (so an
upgrade can never leave an old version running); if the server cannot be reached you get a short note saying so. The pages were checked at phone width: nothing scrolls sideways,
the navigation scrolls along the top, and the 3D view fits the screen.

## Printers

**Print Queue → Printers** (administrator only). Add a printer by its address:

- **Klipper / Moonraker** (Mainsail, Fluidd...): e.g. `http://192.168.1.60:7125`; the API key is optional (only if you set one).
- **OctoPrint**: e.g. `http://octopi.local`; the API key (OctoPrint settings → API) is required.

Each printer shows its state, the file and progress, and the nozzle and bed temperatures, refreshed every few seconds while the tab is open. **Send a G-code file** uploads a file
you choose to the printer; **Start printing as soon as it arrives** is off by default and asks for confirmation. Printers print G-code, not models, so sending a *model* needs slicing:

- Give the container a headless slicer and the printer profile exported from it, for example PrusaSlicer or OrcaSlicer's command line:
  `-e SLICER_CLI_PATH=/slicer/prusa-slicer -e SLICER_CONFIG_PATH=/slicer/printer.ini -v /mnt/user/appdata/slicer:/slicer:ro`.
  Both must be set. Without your profile the slicer would use defaults that do not match your machine, and the wrong G-code can damage a printer, so slicing stays off.
- A model's page then shows **Slice and send** (infill percentage, optional start). Slicing runs the slicer as `<slicer> --export-gcode --load <profile> --fill-density N% -o <out> <model>`
  (the PrusaSlicer family's command line).

API keys are never shown again after you save them, are never in an error message, and are left out of backups (a restore keeps the key of a printer that is the same printer).
**Status of testing:** the Moonraker and OctoPrint code follows their documented HTTP APIs and is tested against simulated printers and a stand-in slicer, and the whole flow was
exercised in a browser against a simulated Klipper host. It has **not** been tried against a real printer or a real slicer, so try it with an idle printer first. Bambu Lab printers
(which use their own protocol) are not supported.

## API tokens

The administrator makes tokens in **Settings → API tokens** (a name, a scope, optionally an expiry). Send one as `Authorization: Bearer mh_...` on any request:

| Scope | Acts as | Cannot |
|---|---|---|
| read | a viewer: looks at everything, changes nothing | |
| write | a member: everything a member can | Settings, backups, users, printers, tokens, duplicate deletion |
| import | the browser extension: only `POST /api/library/import` | anything else |

The key is shown once when it is made; only a SHA-256 fingerprint and its first characters are kept, so a lost key means making a new one. Each token shows when it was last used, can be revoked at any time
(it stops working at once), and tokens are left out of backups and untouched by a restore. A token cannot make tokens. Example: `curl -H "Authorization: Bearer mh_..." http://server:8420/api/stats`.
The interactive API documentation is at `/docs` on your server.

## Behind a reverse proxy (HTTPS)

Model Hub speaks plain HTTP on port 8420. To reach it from outside your network (or to use camera or clipboard features that need a secure page) put an HTTPS proxy in front of it and **expose as little as you can**:
the administrator login protects everything except share links (`/share/...`), which are public to anyone holding the secret link.

- Pass `X-Forwarded-Proto` so the session cookie is marked **Secure** (Model Hub does this automatically when it sees `https` in that header).
- Allow large uploads (model files are big): nginx `client_max_body_size 2g;`, Caddy needs nothing, Nginx Proxy Manager has *Custom Nginx Configuration*.
- Nginx: `proxy_pass http://UNRAID-IP:8420; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; proxy_read_timeout 600s;`
- Caddy: `models.example.com { reverse_proxy UNRAID-IP:8420 }`
- Prefer a VPN (WireGuard/Tailscale) over opening the port to the internet if you only need access yourself. The login has a rate limit but no second factor.
- Unraid: Nginx Proxy Manager, SWAG or Caddy from Community Apps all work; point them at the container's IP and port 8420.

## Versions, sharing and labels

- **Versions:** a model's page has a *Versions* panel. It suggests other files that look like versions (the same shape, or the same name apart from a version suffix such as `_v2`); **Group with this model**
  puts them in one family. Give each a label (`v1`, `final`...). Leaving a group, or deleting a model, dissolves a family that is down to one. Library → Filters → **Latest versions only** shows just the newest file of each family.
- **Share:** the *Share* panel on a model's or project's page makes a link (`/share/<secret>`) anyone who has it can open without logging in. The page is read-only and shows the title, picture, size, designer, license,
  tags, the listing's description and link, *What worked*, and for a project its models, filament totals and parts. It never shows notes or print history, and shows part costs and offers file downloads only if you ticked them for that link.
  Links can expire (1, 7 or 30 days) and **Stop sharing** ends one at once; deleting a model or project deletes its links. Viewers cannot see or make links. Share links are not in backups and a restore leaves them alone.
  **A link works for anyone who can reach this server's address.** To share outside your home network you must expose Model Hub yourself (a reverse proxy or a VPN); the link itself adds no login.
  The page is served with a locked-down content policy (no scripts, nothing external) and every piece of text is escaped.
- **QR labels:** **Filament → Print QR labels for spools** and **Supplies → Print QR labels** make a sheet of labels (QR code, name, a line of detail). Print it, or save as PDF. A label's QR opens
  `#/spool/<id>` or `#/supply/<id>` on this server, which scrolls to and flashes that row. Your phone's own camera app does the scanning, so this works over plain http on your network (an in-app camera scanner would need https).
- **3D view tools** (under the model viewer): **Measure** shows the distance between two clicked points, in the model's units (millimetres for most printable files), with the x/y/z parts; **Section** cuts the model along X, Y or Z with a slider
  so you can see inside; **Compare with...** overlays another version (from its family, or a suggested one) in translucent orange, lined up by their centers, and reports how much bigger or smaller it is. STEP files cannot be compared.

## Library tools

- **Filters** (Library → *Filters, sorting and saved searches*): designer, license, collection, project, linked to a listing or not, has notes, printed or never printed, and
  **Fits my printer's bed**. The bed filter uses the build volume you enter in **Settings → Print estimates**; a model passes when its footprint fits as it is or turned flat
  on the plate (X and Y swapped) and its height fits. Models whose size is unknown (STEP files, failed parses) are left out of that filter.
- **Sort** by name, newest or oldest added, largest or smallest file, or last printed.
- **Saved searches** keep the current filters under a name; saving under an existing name replaces it. Viewers can use saved searches but not create them.
- **Bulk edit**: press **Select**, click models (or **Select this page** / **Select everything that matches**, up to 5000), choose an action and **Apply**. It tells you how many changed and how many already were so.
  Filter choices and bulk edits are for members and the administrator; viewers are read-only.
- **What worked**: per-model print settings (free text fields, nothing is interpreted), kept in backups. **Print again** adds a queue entry copying the filament, grams and minutes of the most recent print.

## Things Model Hub does by itself

One timer runs in the background (it wakes every half minute and runs whatever is due; a failing job never stops the others):

| What | How often | Settings |
|---|---|---|
| Saved backup (`auto-backup-*.zip` in `/config/backups`) | weekly by default | Settings → Backup: weekly / daily / off, and how many to keep (default 7). Other saved copies are never pruned by it |
| New uploads from designers you follow | every 6 hours | notification only; also in the Following tab |
| Low filament and supplies | every 6 hours | Settings → Notifications: warn at this many grams (default 100, 0 = off). Supplies use the minimum you set on each. You are told once per item, again only after it was restocked and ran low again |
| Listing changes | about daily, **off by default** | Settings → Notifications: check linked listings by itself |
| Printers | every 30 seconds | see below |

**Printer finished:** when a printer that was printing stops, Model Hub notices (Klipper reports *complete*; for OctoPrint a print that ended at 100% counts as finished).
If the file was one Model Hub sent for a model, the print is recorded for that model: the waiting print-queue entry is completed (which takes the filament off the spool) or, with no
queue entry, a print-log entry with the print time is written. A cancelled or failed print is announced but not logged. Nothing is ever started or stopped by this, and a print that ends while Model Hub is
restarting is not noticed (the state is kept in memory).

**Notifications** go to the webhook you set (ntfy, Discord, Slack...). **Settings → Notifications** has a *Send a test message* button and a switch for every kind: new files, AI tagging finished,
new uploads, listing changes, low stock, print finished or stopped, backup failed.

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

`browser-extension/` is a small Manifest V3 extension for **Chrome / Edge** and **Firefox**. It installs in your browser and
talks to your running Model Hub server over the network. It works on **Printables, MakerWorld, Thingiverse, MyMiniFactory,
Cults3D and Sketchfab** model pages, and because it downloads with your own logged-in session it is how you add files from the
sites that don't let a server download them.

**Get it:** Settings → Browser Extension → **Download the extension (.zip)** (it is bundled in the Docker image), then unzip it.
1. **Chrome / Edge:** open `chrome://extensions`, enable **Developer mode**, **Load unpacked**, pick the unzipped folder.
   **Firefox:** open `about:debugging` → **This Firefox** → **Load Temporary Add-on** → pick `manifest.json` (Firefox removes
   temporary add-ons when it restarts, and may ask you to allow the site permissions).
2. In Model Hub, go to **Settings → Browser Extension** and copy the API key.
3. Click the extension icon → enter your Model Hub server URL (e.g. `http://192.168.1.50:8420`) and paste the API key →
   **Save** (grants permission to reach that one origin) → **Test Connection**.

**Use:** open a model page on one of those sites. A **📦 Send to Model Hub** button appears bottom-right. It reads the page's
`schema.org` JSON-LD (title/author/license) and lists the direct model-file links on the page (`.stl .3mf .step .obj .fbx .zip`,
plus each site's own download links such as Thingiverse's `/download:` links), lets you pick files and edit designer/license, then
downloads each one **with your login** and POSTs it to `/api/library/import`. Only model files (or a `.zip` containing some) are
accepted; the server opens a zip and keeps just the models. When the page is on a supported site, the model arrives already
linked to its listing with its pictures and details.

Caveats: some sites only reveal their file links after you press their own Download button, and some require you to be logged in;
open the page that lists the files, then use the button there. The extension avoids hardcoded CSS selectors to stay resilient to
markup changes, but a site redesign can still change what it finds.

**Firefox:** the same folder works in Firefox 128 or newer (it has an add-on id and an event-page background next to Chrome's
service worker; Firefox warns about the unused `service_worker` key, which is harmless). It was tested in real Firefox 157
(headless, driven by Selenium): the add-on installs, the popup's **Test Connection** and **Save** (including the permission
for your server) work, the button and panel appear on a model page, the JSON-LD title/designer/license are read, and a file link
is downloaded and imported into Model Hub with the right name, designer, license and source link. Not covered: logging in to
the real sites and downloading real files from them (that depends on your own account), and Firefox for Android.

## Architecture

- **Backend**: FastAPI + SQLModel (SQLite) — `app/main.py`, `app/routers/*`
- **Scanning**: `app/scanner.py` walks the mounted library, hashes files, detects duplicates. Runs in a worker thread (not the event loop) so the WebUI and `/api/health` come up immediately even on a large first-time index; mesh/thumbnail processing is bounded (capped convex-hull/render face counts, chunked hashing) to keep memory flat on very large libraries; only one scan runs at a time (a second `Rescan Library` request gets a clean `409` instead of a SQLite lock error)
- **Mesh worker**: `app/mesh_worker.py` — every mesh file is parsed in a separate, memory-capped worker process, never in the web server. trimesh needs many times a file's size to load it (a 35MB Bambu Studio project file peaked at 4.1GB; a 262MB STL at 2.9GB), so a single big file used to get the whole app OOM-killed on every scan. Files estimated to need more than the worker's budget are indexed from a preview instead of a full parse (a `.3mf`'s embedded slicer thumbnail; for binary STLs, a sampled render plus exact face count and bounding box read straight off disk). If a worker still dies or hangs, only that file fails and the worker is replaced. Budget defaults to half of the container limit / host RAM (1-8GB); override with `MESH_WORKER_MEMORY_MB`. A marker in `/config` also breaks any crash loop: a file two consecutive scans died on is indexed without analysis from then on.
- **Thumbnails**: `app/thumbnails.py` via trimesh (headless render, matplotlib fallback); also computes volume/watertightness for print estimates. `.3mf` files use their own embedded slicer-rendered preview when present; `is_watertight` is skipped past `MAX_WATERTIGHT_CHECK_FACES` since it scales with face count and multi-plate project files concatenated into one mesh can reach several million faces. The live viewer asks before loading models over 2M faces (or very large files) rather than risk freezing the browser tab
- **Viewer**: `app/static/app.js` + Three.js for STL/OBJ/3MF/FBX; STEP/STP tessellated client-side by `occt-import-js` (WASM OpenCascade) into a Three.js mesh. `3MFLoader.js` resolves component references across all of a multi-part `.3mf`'s model parts, not just the one the reference appears in (needed for Bambu Studio/OrcaSlicer project files, which split each plate/object into its own part file)
- **AI**: `app/ai/` — `OllamaProvider` (local) and `APIProvider` (OpenAI/OpenRouter-compatible), selected per the `ai_mode` setting
- **Auth**: `app/auth.py` + `app/routers/auth_router.py` — session cookie for the WebUI, a separate narrower-scoped API key for the browser extension, enforced by a single ASGI middleware in `main.py`
- **Print estimates**: `app/estimate.py` — volumetric heuristic by default, or exact numbers via an optional external slicer CLI (`SLICER_CLI_PATH`)
- **Notifications**: `app/notify.py` — generic webhook POST, best-effort
- **Migrations**: `app/db.py` auto-adds new columns to existing SQLite tables on startup (no Alembic; fine for this project's size, but note it if you fork it)
- **Pages**: hash routes (`#/library`, `#/projects`, `#/model/<id>`, `#/project/<id>` ...), so every model and project has an address that survives a reload and works with the back button. The 3D view's WebGL context and GPU memory are released when you leave a model page.
- **PDF export**: `app/project_pdf.py` (ReportLab), using the DejaVu fonts that ship with matplotlib so accented and non-Latin text prints correctly.
- **Cache busting**: `index.html` is served with `no-cache`, and its own scripts/stylesheet get a `?v=<content hash>` token computed at startup, so a new release always loads fresh JS/CSS; everything under `/assets` is sent `no-cache` so browsers revalidate (cheap 304) rather than reuse stale copies
- **Frontend**: vanilla JS + Three.js, no build step (`app/static/`) — mobile-responsive down to phone widths (scrollable tab bar, stacked toolbars/forms, full-screen viewer modal). The Library view is paginated (`app/static/library-controls.js`), with search/tag filtering applied at the database-query level so it covers the whole indexed library, not just the current page

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
