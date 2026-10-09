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
| Sliced files per model | Done — keep G-code or a sliced `.3mf` with a model (a *Sliced files* panel on its page); Model Hub reads the slicer's time, filament weight, filament type and layer height from the file, and can send a kept G-code file to a printer without any slicer in the container. See [Sliced files](#sliced-files) |
| Recent changes and undo | Done — **Library → Recent changes** records who changed what; bulk edits can be undone. See [Recent changes](#recent-changes) |
| Wishlist | Done — a **Wishlist** tab keeps listings you want for later (save from a search result or a listing page, with a note), shows which are already in your library, and **Add all to library** queues every one the server can download in one go; the rest are reported with the reason. See [Wishlist](#wishlist) |
| Choose which files to download | Done — a listing's page lists every file (Printables' individual STLs as well as its model pack, Thingiverse's files, ...); tick the ones you want, or leave the default (the pack / all model files) |
| Wikimedia Commons, NASA 3D Resources and Smithsonian 3D | Done — three more sites that need no account and let the server download: Commons' 3D files, NASA's public-domain 3D Resources (Apollo landing sites, satellites, ...) and the Smithsonian's print-ready museum models (fossils, corals, Apollo hardware). All are searchable and downloadable from the Search tab; they are left out of the whole-library match job, since a personal file is unlikely to come from there |
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
- The job goes easy on the sites (about one search pair per second) and can be stopped any time. A site that
  fails three times in a row is **left out for the rest of the run** (the message names it and says why) while the
  other sites carry on; a model that one site could not search and the others found nothing for is *not* recorded
  as "no match", so the next run tries it again. The job stops by itself only if every site keeps failing or a site
  rate limits. It remembers what it has checked, so running it again only looks at new models; tick **Also search
  again for models that had no match** to retry those too.
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

**Which sites let a server download files:** Printables (no login needed), Thingiverse (with your token), Wikimedia Commons,
NASA 3D Resources and Smithsonian 3D (all open, no account). MakerWorld,
Sketchfab and MyMiniFactory only give files to a logged-in user (MyMiniFactory's API key alone can't download), and Cults3D's
API never serves files at all. For those, the page says so and points to the **browser extension**, which downloads in
your own logged-in browser. Downloads are restricted to each site's own domains (every redirect is checked), the token is only
sent to Thingiverse's own hosts, and a single file is capped at 1.5 GB. Sites are searched in parallel, so one slow site
delays the results by its own time rather than adding up.

**Other sites looked at:** *Wikimedia Commons*, *NASA 3D Resources* and *Smithsonian 3D* were added because they have open interfaces that I
tested against the live sites. Smithsonian 3D has a public 3D API (no key); only the models it offers as print-ready STL are searched (about a hundred,
the rest of its collection is made for screens), and a model page link from 3d.si.edu is recognised. Its licence is shown as "CC0 where the listing says so",
because the API does not say which models are CC0: check the listing. I also went through the sites in Makerspaces.com's list of 45 download sites
(from 2015): Thingiverse, MyMiniFactory, Cults3D, Sketchfab and NASA were already here; *NIH 3D* has no public API any more (its search runs on private
calls and the site is being reprocessed); *YouMagine*'s search is script-driven; *GrabCAD*, *Embodi3D*, *STLFinder*, *3D ContentCentral*, *SketchUp 3D
Warehouse* and the paid marketplaces (*CGTrader*, *TurboSquid*) need a login or block servers; *Yeggi* and *STLFinder* only point at other sites; and *Repables*,
*Libre3D*, *Pinshape*, *Ponoko* and *Wevolver* are gone or not model libraries. So none of those were added rather than guessing at scraping; if one of them
publishes an API later it is a small addition. For any site that needs a login to download, use the browser extension.

**Internet Archive (Thingiverse archive)** was added from the research for this release: the Archive keeps a copy of a large part of Thingiverse (about 600 000 things, one item each, with the thing's ZIP, description,
designer and Creative Commons licence). Its search and downloads need no account or token, so it is a way to reach Thingiverse things without a Thingiverse token; it is only that archive that is searched, not the Archive's other
collections. I also tried *GitHub repositories* (search by topic): the results were mostly tools and slicers, not models, so it was left out. Features that other tools are praised for, and that this release adds: AMS reading,
maintenance schedules, open in slicer, a weekly summary. Ones I looked at and left for later: order tracking for people who sell prints, NFC tags for spools (the QR labels do the job from a phone camera), and staggered start for
farms with limited power.

**Research for versions 2.16 to 2.19:** I asked what else people want from a self-hosted 3D-print library and what other tools are praised for, and built the
ones that fit this program: every spool of a multicolour print, pause/resume/cancel, bed sizes and "will it fit", off-site backups, a cost calculator, orders for people who sell prints, choosing the printer, Spoolman, watching a camera for a failed print (with your own local vision model,
not a service), mesh check and repair, sharing from a phone, creator pages, drying reminders, where the disk space goes, smart-plug energy, filament for part-printed failures, starred models, collection covers, NFC tags for spools, staggered starts with a power limit, and an optional pause when the camera sees a failure (it never cancels, because a wrong guess would ruin a good print). A further research round (a multi-source search checked claim by claim) added queue priority, hold and tags, the low-spool check, filament weight from G-code length, the spool database and the shop-order import. Left out on purpose: *ActivityPub federation* (a large project with no sign of demand here), mounting external folders (the library folder is already read in place) and direct shop connections (credentials I cannot test). *NIH 3D* has no documented interface (its site runs on private calls), so it was not added; *Sketchfab* and *MyMiniFactory* downloads need your own login, which is what the existing sources already ask of you. A second round (also checked claim by claim, mostly from Bambuddy's and Manyfold's release notes and issue trackers) produced the budgets, a sliced file per printer, the exact-colour switch, sensor holds, bulk printer actions, the queue timeline with "if started now", the printer role and the most-used sort, all described above. On the source side it looked at *Thingi10K* (10 000 meshes, but a frozen 2009-2015 Thingiverse subset, so it adds little) and *Objaverse* (800 000 objects, 8.9 TB, mostly textured Sketchfab models with a licence field each: usable only as a metadata index, so it was not added). No other site with a usable interface was verified. Thingi10K and Objaverse (earlier set aside as frozen or metadata-only) were added after all as opt-in sources, and a slicer-facing virtual printer. A fourth round (the same checking) found, in this order: a Prometheus /metrics endpoint with a Grafana dashboard; a Telegram bot and other push targets (ntfy, Pushover, Gotify, Matrix, Bark); structured fields in webhook messages; a *plate clear* signal over MQTT; OIDC/LDAP sign-in with two-factor and an audit log; marketplace order sync (Etsy, eBay, WooCommerce, ShipStation, TikTok Shop); automatic restocking from the shelf and a customer quote page; camera detection of an occupied plate; and two more sources, the ABC Dataset (about 1 million Onshape CAD models, heavy and uncurated) and NIH 3D (no documented API). A third round (checked the same way) produced the report, the extra maintenance triggers and Bambu fault codes, the Discord additions, the shelf of finished parts, the stored files and the tougher 3MF reading. It could not verify any new model site (Thangs, Creality Cloud, Prusa, YouMagine, GrabCAD and the rest of the list stayed unverified, so none was added), nor demand claims from Reddit or issue trackers; nearly everything came from vendors' own feature pages. Not done: per-user wallets (budgets are shared cost centres) and a slicer-facing virtual printer. More model sources were searched for as well (museum, scan and asset libraries such as Europeana, MorphoSource and Poly Haven):
their models are mostly made for screens, not for printing, or need an account or a key, so none was added.

**A fourth research round (versions 2.26 and 2.27)** added Prometheus metrics with a Grafana dashboard, Telegram, Pushover, Gotify, Matrix and Bark with quiet and loud messages, a Telegram bot, the plate-cleared wait, and, in 2.27, sign-in hardening and shop orders. Not built: the ABC Dataset (its files are 4.65 GB archives of STL chunks, too heavy to offer as a search) and the NIH 3D Print Exchange (no documented API).

Seven more lists of free-model sites (WeNext, Phrozen, Kingroon, eufyMake, 3Dprinting.com, 3Dnatives and Creality Cloud's own tag pages) added no new site. Their picks are Printables,
MakerWorld, Thingiverse, MyMiniFactory, Cults3D, Sketchfab, NIH 3D, Smithsonian and NASA (all dealt with above) plus: *Thangs* and *Free3D* refuse requests from a server (403) and have no public
API; *Creality Cloud* has no public API either (its pages run on a private app interface, and downloads need a login, which is the case where the browser extension is the way); *3DExport*, *3DSky*,
*Toymakr3D*, *Make It Real*, *Pixup3D*, *Nexprint*, *Fab365* and *Brutal Cities* are shops, galleries or login-gated libraries with only web pages; *Iteration3D*, *Yeggi*, *STLFinder* and *3DFindit* are
search engines over other sites; and *Instructables* is a project site whose models sit inside write-ups. I looked each up on 2026-10-08.

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

**Failed prints.** Mark a print as failed (its *How it went* choice in the print history, or the same choice when logging one) and say why: it would not
stick to the bed, warped, came loose, layers shifted, clogged nozzle, ran out of filament, supports failed, bad quality, power lost, you stopped it, or
something else. A queue entry set to *failed* asks why; a print a printer reports as stopped is logged as a failure by itself (with the print time and, if the
printer has a camera, a picture), and the reason is yours to add. A failure is kept but is **not a print**: it does not count as the model having been
printed, does not take it off the never-printed list, and does not teach the learned print times. Nothing is taken off a spool for a failure until you say how many
grams it used (edit its entry), and then the **Stats** page can show how much filament and money failures wasted and which reasons are most common.

**What went wrong before.** A model that has failed shows a box at the top of its print history ("Failed 3 of 5 attempts, usually: Warped or curled", the materials the
failures were on, and a tip for the usual reason), and the same warning appears next to it in the Print Queue and on the Calendar, so you hear about it before you start
it again. The Library can be filtered to **Failed before**. The tips are general advice, not a diagnosis.

**What worked, from how it went.** *What worked* on a model's page now adds a suggestion made from its prints: the material it printed best in (a better rating counts for more), the layer height and profile of the
sliced file that printed successfully, and materials to stay away from (two or more failures, at least half of the attempts). *Fill in the empty boxes* copies it into the form; nothing is saved until you press Save.

**Time-lapse links.** A print entry can keep a link to its time-lapse video (**Time-lapse**, then paste the address). For a print a printer reported, the administrator
can press **Find on the printer**: Klipper (with the moonraker-timelapse plugin) and OctoPrint are asked for their time-lapse videos, the ones saved closest to the
print come first, and *Use this one* keeps the link. The link opens in a new tab, and **Play here** plays it inside the page (not offered when Model Hub is served over https and the video is on plain http,
which a browser would block). Videos are never copied; only the address is kept.

**Photos from a phone.** *Take photo* on a print entry opens the phone's camera straight away (*Choose photo* picks from the gallery); the Calendar's
list of prints made on a day has the same button, so the picture can be added right when the print comes off the bed.

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

## Sliced files

On a model's page, **Sliced files** keeps the G-code (`.gcode .gco .g .bgcode`) or sliced `.3mf` you made for it, with an optional note. Model Hub reads the comments the slicer wrote:

| Slicer | What is read |
|---|---|
| PrusaSlicer, OrcaSlicer | slicer and version, estimated printing time, filament used (g), filament type, layer height |
| Bambu Studio | slicer, total estimated time, total filament weight, filament type, layer height (also from a sliced `.3mf`'s `Metadata/slice_info.config`) |
| Cura | slicer, `;TIME`, layer height (Cura does not write grams) |

Binary `.bgcode` files are stored and sent but not read. Nothing here runs a slicer, and what is read is shown, not trusted: you can correct it through the API. A kept G-code file can be **sent to a printer** (Print Queue's printer
setup is needed; the administrator only) with the *Send to printer* button, optionally starting it. When that print finishes, the print log records the file's estimated grams (or completes the waiting queue entry), as described under
[Things Model Hub does by itself](#things-model-hub-does-by-itself). Files are stored in `/config/print_files`, are **not** in backups (they can be large and can be made again; a restore leaves rows that say *file missing*),
are deleted with their model, and the same file is kept once per model. The limit is 1 GB per file and 50 per model.

## Recent changes

**Library → Recent changes** lists who changed what, newest first, and can be filtered by person or kind. It records bulk edits, models removed from the library index, duplicate clean-ups, backup restores, logins added/changed/removed, API tokens,
share links, printers, and the *names* of settings that changed. It never records passwords, keys, tokens or setting values. The newest 2000 entries are kept, and the list is part of a backup.

**Undo** is offered for bulk edits (tags, collections, projects, designer, license, print queue), right after you apply one and from this page. An undo only touches what the edit changed and leaves anything changed again since alone
(a designer you edited afterwards is not overwritten; a queue entry that has already started stays). Members and the administrator can undo; viewers cannot. Each undo is itself recorded, and an edit can be undone once.

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

## Filing rules

**Collections → Filing rules** files a model for you when it is linked to an online listing. A rule says: when the listing's *category*, *tags*, *title* or *designer* contains some text (at least 2 letters, any case),
add a tag, or put the model in a collection (created by that name if it does not exist). Rules run when a model is linked or its link is refreshed, and **Run on all linked models** applies them to the whole library
(**Preview** only counts). Rules only ever add. The run is recorded in Recent changes and can be undone there or right after. The listing's category is known for models linked from now on.

## Print queue for several printers

In the Print Queue, an entry can be assigned to a printer (the printers set up under Settings → Printers; only the administrator sees the printer choice). The top of the queue shows each printer's waiting jobs and total estimated time
(entries without an estimate are counted separately). **Send** (administrator only) uploads the model's newest kept G-code file to that printer, after asking whether to start it; it refuses a printer that is offline or busy
and an entry that is already printing or done. When that printer finishes, the entry assigned to it is the one completed. Model Hub never starts a print you did not ask for.

## Library information, export and import

**Settings → Library information** exports what Model Hub knows about your models (tags, collections, projects, designer, license, notes, the listing each came from, print count; not the files) as JSON or a CSV spreadsheet,
and imports either back, or a spreadsheet made elsewhere. Rows match your models by content hash, then path, then a file name that is unique. Import only fills what is empty (tick *Replace* to overwrite designer, license
and notes), creates missing tags and collections but never projects, and links a listing only for a known provider and id. **Preview** shows what would change. The file may be up to 100 MB; spreadsheet cells that begin with
`=`, `+`, `-` or `@` are stored with a leading `'` so they can never run as a formula. An import is recorded in Recent changes.

## Home Assistant and MQTT

**Settings → Home Assistant and MQTT** publishes to an MQTT broker (host, port, optional user and password, optional TLS, topic prefix `modelhub`). Leave the address empty to keep it off.
Every 30 seconds it publishes, retained: `<prefix>/printer/<id>/state` (name, online, state, progress, file, nozzle and bed temperature, taken from the printer check that already runs) and `<prefix>/stats`
(models, never printed, prints this month, low stock, filament remaining in grams, queued jobs, update available). With *Home Assistant discovery* on, matching entities appear by themselves (hourly and whenever printers change).
Model Hub only publishes: it listens to nothing and cannot be controlled over MQTT. The password is hidden in Settings and left out of backups. *Send a test message* checks the connection; the last error is shown there.

## Pictures for STEP files

The server cannot draw STEP files, so the browser does: **Library → Make STEP pictures** renders each STEP model that has no picture (using the same viewer engine) and sends a small PNG to the server. It works on this
computer's browser while the page stays open; models the browser cannot read are skipped and counted.

## Print times that learn

Estimates are only guesses, so Model Hub keeps score. When a printer reports a finished print, or you type a time into a print-log entry, that time is remembered as *measured*. Then:

* **A model you have printed before** is queued with the median of its measured times ("from your past prints").
* **A model you have not printed**, with a time from its sliced file or the Estimate button, is corrected by how far off estimates usually are, once at least three finished queue entries have both an estimate and a real time
  (never more than 3 times or less than half). The Print Queue says so ("prints usually take 20% longer than estimated").
* A time you type yourself always wins and is never changed.

Nothing is changed after the fact: the number is only filled in when an entry is added. The model page's *Estimate print* shows what your own prints suggest next to the usual estimate.

## Photos from the printer's camera

Give a printer its camera's still-picture address (Print Queue, the printer's *Camera* section; for Mainsail/Fluidd it looks like `http://host/webcam/?action=snapshot`). When that printer finishes a print, the picture is kept as the photo of the print-log
entry, unless the entry already has one. *Take a picture now* tests the address. The address must not contain a user name or password; redirects are not followed, the printer's API key is never sent to the camera, and a camera that is off or wrong never
stops the print being recorded. Photos are kept in `/config/print_photos` and are part of backups.

## Cost to print, and what to ask

A model's page has **Cost to print**: grams and minutes (from its kept sliced file or last print, and the learned print time; type your own to replace them), how many, and which spool. It adds the filament (the spool's price per gram, or the average of that
material's spools, or a default price per kg), electricity (the price per kWh and the printer's watts), machine time (what an hour of the printer is worth to you), an allowance for failed prints, and your margin, and shows the cost of one, the price to ask, and the same for
several. The failure allowance is your own failure rate once you have logged 10 prints, otherwise 5%, unless you set a figure. All the numbers are set in **Settings → Costs**; anything missing (no spool price, no price per kWh) is said, not guessed.

## Orders

**Orders** keeps prints made for other people: a customer, contact, due date, notes, and the models with how many of each and the price (left empty, the cost calculator's suggestion is used). **Put the prints in the queue** adds one waiting print per unit not queued yet
(planned for the due date; a failed print is made again), progress follows the queue (*3 of 5 printed*), the order says when it looks ready, and totals show price, cost and profit. Orders can be marked paid and delivered, late ones are flagged, and **Orders as a spreadsheet** downloads
them (cells that look like formulas are stored as text). Deleting an order leaves its prints in the queue.

## Choosing a printer

A waiting print with no printer shows the printer that suits it best and why: it must fit the bed (when the bed size is known), a printer that has the print's spool loaded (in a slot) scores best, one with the same material next, and printers with less already waiting rank higher.
*Use* assigns one (and its slot); *Give the waiting prints … the best one* assigns them all and can be undone.

## Spoolman

**Settings → Spoolman**: enter its address (like `http://192.168.1.20:7912`). **Import** brings its spools in (once each; a later import refreshes the remaining weight), **Send mine to it** creates Model Hub's spools there (a vendor, a filament and a spool each, once each), and with
*Report each print's grams* ticked every finished print also tells Spoolman how much it used, so both keep the same count (do not also let a printer report usage to Spoolman, or it is counted twice). Nothing is deleted on either side.

## Watching a camera for a failed print

On a printer's card, **Camera**, tick *Look at the camera now and then*. While that printer is printing, every two minutes one picture goes to your own **local vision model** (Settings, AI mode *local*, the vision model you set; nothing goes to a paid service), which says whether the print
looks failed (loose strings, a part knocked off, a blob). Two bad looks in a row send one **A camera thinks a print may have failed** notification (and not again for that print). It only tells you; it never pauses or stops a printer. A vision model can be wrong both ways, so it is a hint.
**Ask the model about the picture now** tests it. It is off for every printer until you switch it on, and needs the camera picture address.

### Pausing a suspected failure

Under the watching switch, *Also pause the print after three bad looks in a row* lets Model Hub pause the print (Klipper, OctoPrint or Bambu), never cancel it. The warning still comes after two bad looks; the pause after a third, six minutes in, and you are told it
was paused (or that it could not be, and why). Resume or cancel from the printer card once you have looked. It is off by default and can only be switched on for a printer that is watched; a vision model can be wrong, so a pause costs you a click, never a print.

## Budgets

**Settings → Budgets** keeps cost centres: a project, a customer, a club account, each with an amount (in all, or a month) and an optional *stop when spent*. Choose a budget on a waiting print (or on an order, which gives it to every print it queues). A queued print **reserves** its expected cost (from the cost calculator: grams and minutes
set on the entry), a finished print is **charged** what it cost, a failed one only the filament that went into it, and you can write in anything else (*Add spending*, or a credit as a negative number). What is left is the amount minus what was spent minus what is reserved. A budget set to stop refuses to queue, assign or send what would take it
over, and starting a print for an over-budget budget cannot be forced; raise the amount instead. A budget that is not set to stop only shows the figure. Prints with no known grams or time reserve nothing and are counted as "not priced". Deleting a budget frees its prints and forgets its ledger.

## A sliced file for one printer

When you keep a sliced file with a model you can say it is **for a printer** (a file made for one machine should never go to another). Sending from the queue uses the printer's own file first, else a file made for any printer, and refuses a model whose files are all made for other printers; suggestions only name printers that have a file.
Removing a printer turns its files back into files for any printer.

## Exact colour

A waiting print can ask for an **exact colour**: only a printer with a spool of exactly the same material and colour (by name or hex) loaded is suggested, and starting it elsewhere asks first. Each print chooses *as in Settings*, *exact* or *any*; **Settings → Print planning** has the default.

## Orders move on by themselves

An order that is *accepted* becomes *printing* when its first unit is done and *ready* when every unit is (with a notification); a delivered or cancelled order is left alone.

## Holding prints while a sensor alerts

**Settings → Printer sensors** takes your Home Assistant address and a long-lived access token (kept out of backups), and binds sensors to printers: a door contact that is *on*, a chamber thermometer *above* a number. Every half minute Model Hub reads them; while one is alerting, that printer is not suggested for new prints and sending to it waits (the queue says why), and you can still start anyway.
If Home Assistant cannot be reached or a sensor is unavailable, nothing is held.

## Several printers at once

Tick printers in the printers list and use **Pause**, **Resume** or **Cancel the print** on all of them (or *Tick the printing ones*). Each printer is looked at on its own: only a printing one is paused, only a paused one resumed, and one that cannot be reached does not stop the rest.

## Queue timeline

The queue has a **Timeline** view: for each printer, its running print (taking what is left of its estimate) and the waiting prints one after another in queue order, grouped by the hour they finish; held prints and prints without an estimate are listed but not placed. Waiting prints that could start right now show **finishes about HH:MM if started now**.
It is arithmetic over the estimates, so it is only as good as they are.

## A printer login

A **printer** login (Settings → People) can look at everything except the administrator's pages and can start, pause, resume and cancel prints, send files to printers and change its own password, but change nothing else: for the person who runs the farm without being its administrator.

## Most used first

The Library sort **most used** puts the models you print most first: each print that worked counts three, each star two and each time it was queued one.

## A virtual printer for slicers

**Settings → Slicer** lets PrusaSlicer, OrcaSlicer or Cura *send* a sliced file straight to Model Hub, which answers like an **OctoPrint** (or **Moonraker**/Klipper) printer: add a physical printer in the slicer with host type OctoPrint (OrcaSlicer can also use Moonraker), Model Hub's address, and an **API token with write access** (Settings → API tokens) as the API key; the slicer's *Test* button works, then *Upload*. The file is kept with the
model it belongs to (matched by name: a file called `benchy_0.2mm_PLA_MK4.gcode` goes to the model called `benchy`; if two models fit equally, or none does, it waits) and, if the slicer says *Upload and print*, it is put in the queue. **Nothing is ever started**: there is no machine behind it. Files it cannot place wait in the **From the slicer** box on the Print Queue page, where you pick the model
(or, for a sliced 3MF, make a new model from it). Only G-code (.gcode .gco .g .bgcode) and sliced 3MF are accepted, up to 1 GB, and the key opens only these few printer addresses (`/api/version`, `/api/files/local`, `/server/info`, `/printer/info`, `/server/files/upload`), never the rest of Model Hub; a read-only token can only look, and an import-only key is refused. It is off until you switch it on.

## Two research datasets as sources

**Settings → Dataset sources** can download the lists for two research collections, after which they take part in Discover searches (until then they are left out). **Thingi10K** is about 2 000 Thingiverse things from 2009 to 2015 (10 000 meshes; three small CSV files), searched by name, designer, tag and category; every model keeps the licence it had, shown as CC BY, CC BY-NC and so on, and
about half of the meshes are not solid (the mesh check and repair help). **Objaverse** is a very large collection of textured models, mostly from Sketchfab; its labelled part (about 46 000 objects) is searched by category name (*chair*, *teddy bear*), the name, designer, picture and licence come from Sketchfab, and a downloaded GLB is converted to an STL scaled to **80 mm along its longest side** (the scale is a choice, not a measurement).
Many Objaverse objects are meant for screens, not printing, and some licences forbid selling prints or changing the model (NC, ND), so the licence is shown on every result. Only the two Hugging Face repositories are contacted (every redirect checked), over https and with size caps; Objaverse's list is a one-time download of about 20 MB.

## Prometheus and Grafana

**Settings → Prometheus and Grafana** switches on `GET /metrics` (off until you do; add a token and the scraper must send it as `Authorization: Bearer ...`). It offers printer up/state/progress/temperatures, prints by printer and result, filament grams, print hours, queue entries, orders, spool remaining grams, stock on hand and low, maintenance due and due soon, and the number of models, in Prometheus text format. **Download the Grafana dashboard** gives a JSON file to import
(Dashboards → Import; pick your Prometheus data source). It is not meant to be public: only switch it on where the scraper is on your own network.

## More ways to be told

Beside the webhook, **Settings → More ways to be told** can send to **Telegram**, **Pushover**, **Gotify**, **Matrix** and **Bark**; each is used as soon as it is filled in. Messages have a loudness: a print started or passing a step is *silent*, most things are *normal*, and a suspected failure or a failed backup is an *alarm* (Pushover priority, Gotify priority, Bark time-sensitive, a Telegram notification that is never muted). The webhook now also carries fields
a script can use (`event`, `printer`, `filename`, `duration_minutes`, `level`, `timestamp`) and an ntfy `Priority` header, next to the text it always had. Tokens and keys stay on the server and are left out of backups.

The **Telegram bot** answers only the chats you list: `/status` (every printer), `/queue` and `/help` always, and, only if you tick *Let the bot pause, resume and cancel prints*, `/pause NAME`, `/resume NAME` and `/cancel NAME` (a cancel must be confirmed with `/confirm` within a minute). It asks Telegram for messages (long polling), so nothing needs to reach your server from outside. Tick *Keep one status message up to date* to have a single message edited in place instead of many;
a forum **topic id** sends everything to that topic.

## Waiting for the plate to be cleared

With **Settings → Print planning → wait for the plate** on, a printer whose print finished or was stopped is marked *plate not cleared* until you press **Plate is clear** on its card, call `POST /api/printers/ID/plate-cleared` (Home Assistant or a script; the printer login may do it too), or a new print starts. While it waits, starting a print on it asks first (you can always start anyway), the state is published over MQTT (`.../plate_clear`, with Home Assistant discovery) and an optional
*needs its plate cleared* message is sent. With a camera and a local vision model, a printer can also be set to **look at the plate** after a print and clear itself once the plate looks empty twice in a row; the model only says what it sees, and an uncertain answer counts as *occupied*.

## The report

**Stats → Report** shows any stretch of time (the last 30 days to start with) with filters for printer and material: print jobs, print time, success rate (finished over all prints), filament used, prints per day, the average print, how long the printers ran, and the cost of the prints (filament, electricity and machine time from Settings, Costs; a failed print costs only its filament). Below are a jobs-per-day chart and tables by printer
(with how much of a day's working hours each one was used), by material, by customer (from the orders the prints were made for), the most printed models and why prints failed. A print with no spool or price known counts as zero money, so the figures are a floor.

## More ways for maintenance to fall due

A maintenance task can also fall due after so many **grams of filament**, **finished prints** or **failed prints** (optionally only failures with one reason, such as *clog*), counted from the last time it was done. Each task shows its progress (*312/500 g*) and, from how much the printer was used over the last 30 days, **expected due about** a date.
**Report a problem** makes a task due now whatever its schedule says, until you mark it done. For Bambu Lab printers, **Settings → Bambu fault codes** ties a code the printer reports (HMS_0300_0100_0001_0001 and so on) to a task by name: when the printer reports the code, that task is flagged as having a problem and you are told.

## Discord and your own wording

A Discord webhook gets a **camera picture** attached to the messages about a print (needs a camera address on the printer; other webhooks just get the text). Three more events exist and are **off until you switch them on**: *a print started*, *a print passed another step of its progress* (every 10% unless you set another step) and *a print was paused or resumed*.
Each of those, and *a printer finished a print*, can have your own wording with `{printer}`, `{file}`, `{progress}` and `{minutes}` filled in; nothing else in the text is evaluated. Nothing is announced the first time Model Hub looks at a printer that is already printing (it was just restarted), and 100% is left to the finished message.

## Finished parts on the shelf

**Orders → Finished parts on the shelf** keeps how many finished prints of a model you have and the fewest you want. *Make more* queues the shortfall (what the minimum asks for, less what is on the shelf and what is already being printed), and every such print adds one to the shelf by itself when it finishes (once, however often its status is changed). **Take from the shelf** on an order fills what it still needs from the shelf, and
queues only the rest; an order filled entirely from the shelf is ready at once. A part at or below its minimum is announced once (and again after it was restocked and ran low again).

## Other files, and what Model Hub can open

A model's **Other files** keeps the files it was made from or for that Model Hub does not open: a Fusion 360, SolidWorks, Inventor or FreeCAD project, a Blender or SketchUp scene, an OpenSCAD source (parametric libraries such as BOSL2 are BSD-2-Clause licensed, so they can be kept alongside), a Lychee or Chitubox slicer project, a PDF or a note. They are kept and downloadable, labelled as *stored only*, never opened,
up to 512 MB each and 50 per model, and not part of a backup. Programs and scripts (.exe, .sh, .html and similar) are refused, and model files go through Import. A small table on the panel says what is opened (STL, OBJ, 3MF and FBX get a picture, a 3D view and the mesh checks; STEP gets a picture and a 3D view in your browser but no mesh checks).

## Tougher mesh reading

A 3MF that the general loader cannot read (slicer *project* files whose parts live in other files inside the 3MF, nested components with transforms, extra plates) is now read directly from its XML, so it still gets a picture, a size and the mesh checks; a file that cannot be read at all is still indexed, and the mesh check answers *no surface* instead of failing. `python tools/corpus_check.py <folder>` runs the same loading and checks over a whole folder of models
and reports what fails, for stress-testing on a big messy collection such as Thingi10K (10 000 meshes of which about half are not solid, 45% self-intersect and 22% are non-manifold; each model keeps its own licence, so use it for testing only).

## Maintenance history

Pressing *Done* on a maintenance task now asks for an optional note (what you used, what you found) and adds a line to **History of what was done** under the maintenance list: the date, the printer, the task, the printer's print hours at that moment and your note. The history stays when a task is removed or its schedule
changed, and goes only when its printer is removed (or you delete an entry with the cross).

## Print profiles

A model's *What worked* box can now use named **profiles**: a saved set of material, layer height, temperatures, speed and so on (for example "PETG 0.2 draft"). *Save as profile* keeps the settings in the boxes (never the notes) under a name; choose a profile and *Fill empty boxes* puts its values only where the model has none,
*Apply all* overwrites the boxes the profile has. Profiles belong to the whole library, so a setup you found once can go on any model; deleting a profile leaves the models that used it as they are.

## Status page for a wall display

**Settings → Status page** can switch on a page for a tablet or monitor: each printer's state, progress and temperatures and how many prints are waiting or on hold, refreshed every 10 seconds, with no sign-in. It is off by default. The link contains a secret (anyone who has the link can look, and *Make a new link* cancels the old one); it shows only what
the background poll already knows (it never contacts a printer itself) and never an address, key, serial number or camera link, and the file name of each print only if you tick the box. The link is kept out of backups and out of the settings list.

## Queue: priority, hold, tags and order

Each waiting print has a **priority** (high, normal, low), a **hold** box and an optional **needs tag**. A held print is not suggested, assigned or sent until you release it (useful when someone must look at it first). Printers can carry **tags** (a room, a group, a nozzle size: printer card, *Tags*);
a print that needs a tag is only suggested for, and only sent to, a printer that has it. **By priority** puts high priority first and keeps the order they were added; **Shortest first** orders by estimated time inside each priority, but anything that has waited two days or more goes first of its priority, so a long print
is never starved. A suggested printer whose loaded spool is nearly empty for that print ranks last.

## Filament from the length, and a low-spool check

When a kept G-code file has no weight in it (some slicers only write the length), Model Hub works the weight out from the length of filament, the filament's diameter (1.75 mm unless the file says otherwise) and the material's density (PLA 1.24, PETG 1.27, ABS 1.04 and so on), so it is a good estimate but not a measurement.
Starting a print from the queue now checks the spool: if it holds less than the print is expected to use, Model Hub says how much is left and asks before starting anyway. Sending without starting is not checked.

## Spool presets (Open Filament Database)

On **Filament**, *Find a spool in the Open Filament Database* searches a community list of about 14 000 colours (brand, material, colour, hex colour and the spool weights sold), kept by the Open Filament Collective and published under the MIT licence. Press *Download or update the list* once (about 6 MB, only from
api.openfilamentdatabase.org; nothing is sent there); then search by any words (*prusament galaxy black*) and *Use* fills the form, colour included. The list is only a convenience: you can still type any spool by hand.

## Importing a shop's orders

**Orders → Import orders from a shop's export** reads the CSV you can download from Shopify, Etsy, WooCommerce, eBay and most other shops (it recognises their usual column names: order number or id, customer or shipping name, email, item name or title, SKU, quantity, price). Each order number becomes one order (status *accepted*);
each line is matched to a library model when its SKU equals the model's file name, or when its title and exactly one model's name contain each other (a title that fits several models is never guessed). Lines that match nothing are listed in the order's notes so nothing is lost, and the same file imported twice makes no copies. *Preview* shows the result without saving. Connecting a shop directly
(its API and webhooks) is not done: that needs each shop's credentials and was not something I could test here.

## Staggered starts and a power limit

**Settings → Print planning** has two limits for farms on a small supply, both off until set: *Minutes between starts* (a print cannot be started within that time of another printer's start) and *Most printers printing at once*. They apply when Model Hub is asked to start a print (the queue's *Send*, or
*Send to printer* with start ticked): it says why it is waiting and, from the queue, asks whether to start anyway; you are always allowed to. Sending a file without starting it, and prints you start on the printer itself, are not affected.

## NFC tags on spools

On a phone that has NFC and runs Chrome (Android), over https, the Filament page gets an *NFC tag* button on each spool, which writes the spool's link to a blank NFC tag, and a *Scan a spool tag* button that opens the spool when you hold a tag to the phone. The tag only holds the link
(like the QR labels), so it works for any phone with the app installed and nothing is lost if a tag is damaged. iPhones and other browsers do not offer the buttons.

## Is the mesh sound, and repairing it

A model's page has **Mesh check**: it looks for open edges (holes), edges shared by more than two faces, flipped or inside-out faces, zero-area and duplicate faces and separate pieces, and says so in plain words. If something can be fixed, **Make a repaired copy** merges duplicates, turns
faces the right way and fills holes flat, saves the result as a new STL next to the original (never replacing it) and groups the two as versions (*original* and *repaired*). It cannot repair what needs design judgement, such as a model that is not really solid. STEP files cannot be checked.

## Sharing a file to Model Hub from your phone

With Model Hub installed on your phone (see *On your phone*), **Share** on an STL, 3MF, OBJ, STEP, FBX or ZIP file offers *Model Hub*, which puts it in the library and opens it (up to 10 files at once). It needs the signed-in account to be allowed to add models, and works on Android
and any browser that supports installed-app sharing; iPhones do not offer this, so use the browser extension or the Import button there.

## Creators

**Creators** lists everyone named as a model's designer (set automatically when a model is linked to a listing, or by hand), with how many models of theirs you have and how much space they take. Opening one shows their models, how many you have printed, which sites they came from and the
licences. A model's page links to *More by* its designer.

## Drying spools

On **Filament**, *opened* and *dried* note the date on a spool. Each material has a rule of thumb (PLA 90 days, PETG, ABS and ASA 60, TPU 30, PC 14, nylon and PA 7, PVA 3, others 90); the spool shows how long ago, *drying due in N days*, or *due for drying*. Once a day, a spool that is **loaded in a printer** and
due sends one **A spool in a printer is due for drying** notification (and again only after it was dried and became due once more). These are general guides: some brands need more or less.

## Storage

**Settings → Storage** shows where the disk space goes: the library by file type, the biggest models, the space taken by duplicates, Model Hub's own folders (thumbnails, source pictures, print photos, kept sliced files, backups, downloads) and the database, and the free space on both disks.

## What a print really used (smart plug)

If the printer is plugged into a **Tasmota** or **Shelly** smart plug that counts energy, give the printer its plug (printer card, *Smart plug*: the make and the plug's address, then *Read it now* to test). Model Hub reads the plug's running energy total when a print starts and when it ends and
keeps the difference, in kWh, with that print (shown in the model's print history). If the plug does not answer, or its counter went backwards, the print is simply recorded without a figure.
The kWh is only shown (the cost calculator still estimates from the printer's watts), so check it against your electricity price yourself.

## Failed prints and the spool

When a printer reports a print stopped or failed, the filament that went into the failed part now comes off the spool: the same share of the job's grams as how far the printer had got (a print stopped at 40% of 100 g takes 40 g), for every spool of a multicolour job, and
the failed entry in the print log shows those grams (deleting the entry puts them back). It is an estimate, so you can still edit the grams. A print with no known progress takes nothing. **Settings → Costs** has a switch to turn this off.

## Starred models

A star on a model's page (and a star on its card in the Library) marks it as one of yours. Every login has its own stars, and the Library filter *Starred* shows only yours (also usable in a saved search, where it always means whoever is looking).

## Collection covers

Each collection shows a picture and how many models it holds. *cover* lets you pick which model's picture stands for it; without a choice it uses its first model's picture.

## Multicolour prints

A kept sliced file (a sliced `.3mf` from Bambu Studio or Orca, or G-code from PrusaSlicer or OrcaSlicer) lists every filament it uses, with its type, colour and grams. On a waiting queue entry, **colours** shows them and lets you say which
slot of the printer (or which spool, for a printer without slots) each is printed from, starting from the slot that holds the same material and a close colour. When the print finishes, each spool loses its own grams (the spool in that slot at that
moment), the print log records the total and the spools used, and the Calendar checks every spool of the plan. *Back to one spool* returns the entry to a single spool. A file whose slicer does not list its filaments cannot be mapped.

## Pause, resume and cancel

The administrator can **Pause**, **Resume** or **Cancel print** from a printer's card (Klipper, OctoPrint and Bambu). Model Hub first asks what the printer is doing and only sends what makes sense (a pause to a printer that is printing, a resume to
one that is paused), and a cancel asks you to confirm. A cancelled print is then logged as a failed print like any other stopped one. Bambu commands are sent through the same LAN MQTT connection as its status and, like the rest of Bambu support, are
untested on a real printer.

## Will it fit

Each printer can have its **Bed size** (width, depth, height in mm). A model's page then says which printers it fits and which it is too big for, a queued entry warns when its model is too big for the printer chosen, and the Library can be filtered to
models that **fit a printer**. The footprint may be turned a quarter turn; the height must fit as it is. Sizes come from the model's measured bounding box, so a model that has not been measured yet is not judged.

## Backups somewhere else

**Settings → Backup → Also send backups somewhere else** copies each new saved backup (including the scheduled ones) to a **folder** (a mounted share or disk, added to the container as a path mapping first) or a **WebDAV** server (Nextcloud, ownCloud, a NAS).
It checks hourly and sends the newest backup once. The folder keeps the newest few (10 by default) and never touches files that are not Model Hub's own backups; WebDAV keeps everything. *Test* writes a small file, *Send the newest backup now* sends one at once. The WebDAV
password is hidden in Settings and left out of backups. Other cloud storage (S3, Google Drive...) is not built in: mount it with a tool such as rclone and use the folder option.

## Bambu Lab printers

Add a **Bambu Lab (LAN mode)** printer with its address on your network, its serial number and its LAN access code (on the printer's screen: Settings, WLAN; LAN Only mode or Developer
Mode must be on). Model Hub asks the printer's own MQTT service (port 8883, the printer's self-signed certificate is accepted because the access code is the proof) for its state, so it
appears in the printer list like the others, a finished or failed print is logged by itself (with the AMS spool it was counted against), and the numbers can go to Home Assistant.
It cannot send files to a Bambu printer (use Bambu Studio) and the access code is kept hidden like an API key. **This was written from the documented protocol and tested against a stand-in,
not against a real printer**, so check the printer card shows its state, and tell me if it does not.

## Which spool is in which slot

A printer with several spools at once (a Bambu AMS has 4 slots, a Prusa MMU 5, a toolchanger one per tool) can be given a number of **Spool slots** (Print Queue, the printer's
*Spool slots* section; 0 to 16). The **Spools in the printers** panel then shows each slot with a menu for the spool loaded in it and an optional name, and the Filament tab says where
each spool is loaded. A spool is in at most one slot: loading it somewhere takes it out of the last one. A queue entry can name the printer and a slot; the spool in that slot at the
time the print finishes is the one the filament is taken from, and the one the Calendar checks the plan against, so a spool swapped in between is the one counted. A Bambu printer
reports what its AMS holds (material, colour and, for spools with a tag, the percentage left), shown next to each slot: *Load the spools it points at* fills empty slots with the spool of the
same material and nearest colour (never replacing one you chose), and *Set remaining weights from the printer* turns the reported percentage into grams. Other printers cannot say what is loaded,
so say it yourself. A print that uses several slots at once (multicolour) is counted against the one slot you chose.

## Print calendar

**Calendar** shows a month: the prints planned for each day (with the time they need), the prints you logged, and the waiting prints that have no day yet. Give a queue entry a day from the Print Queue (the date box on each entry) or from the Calendar.
A day turns red when its planned time is more than your printers can do (**Settings → Print planning**: hours a printer may run per day, 12 by default, times the number of printers you added). *Plan them automatically* puts each waiting print on the first
day from today with room, in queue order (an entry without an estimate counts as an hour; one longer than a day gets a day to itself); *Preview* shows the result first and a real run can be undone from Recent changes or right after.
**Calendar file (.ics)** downloads the planned prints for a phone or desktop calendar. It is a download, not a live subscription, because calendars cannot sign in.

## The calendar per printer, and filament

When you have printers, the Calendar has a **Printer** choice: *All printers*, one printer, or *Not assigned*. A printer's day holds the hours you set
(Settings, Print planning); a print with no printer chosen can go on any of them, so a day with two printers holds twice as much. A day is red when
one printer has more than its own hours, or the whole day has more than all printers together. Automatic planning respects this per printer.
Prints already made are not tied to a printer, so they show only under *All printers*. The **.ics** file follows the choice too.

The Calendar also checks your **filament**. Planned prints that name a spool and a weight are taken in date order, and any print that the spool will not have
enough left for is marked (the day gets a warning, the day's list says by how many grams). Prints already done are not counted again. Once a day Model Hub also
tells you (the *A planned print needs more filament than you have* notification, which has its own switch) about shortfalls in the next two weeks,
once for each, and again only if it was fixed and then became short again.

## Printer maintenance

**Print Queue → Printer maintenance** keeps tasks for each printer (oil the rails, change the nozzle...) that fall due after so many hours of printing and/or so many days; common ones are offered.
Print hours are the minutes of the prints a printer reported (failed ones too), counted from the moment the task was added or last marked **Done**, so a printer Model Hub is not connected to only gets the
day-based part. A task is *soon* from 90% of its interval and *due* at 100%, and you are told once when it becomes due (the *A printer needs maintenance* notification).

## The weekly summary

**Settings → Notifications → Send me a summary of the week** sends one message every seven days to your webhook: prints, hours, filament and cost, failures and their usual reason, what waits in the queue and is planned for
the coming week (and what is short of filament), what runs low, maintenance that is due or soon, and a new Model Hub release. It is off until you switch it on, and starts counting from that day. *Send one now* shows what it looks like.

## Open in a slicer

A model's page has **Open in a slicer** (PrusaSlicer, OrcaSlicer, Bambu Studio). The slicer on your computer is given a link that works for 15 minutes for that one file (signed with this server's secret, read-only, no login needed),
so Model Hub must be reachable from that computer at the address in your browser. It needs a slicer recent enough to open `prusaslicer://`, `orcaslicer://` or `bambustudio://` links; if nothing opens, download the file instead.

## Repeating a week

Select a day in the Calendar and **Repeat this week** copies every print planned in that week (Monday to Sunday) onto the following 1 to 8 weeks, on the same weekdays, as new waiting
prints with the same printer, slot, spool, estimate and notes. Prints already done are included unless you untick that, so last week's batch can simply be run again; failed ones are not.
*Preview* counts without adding anything, and a real run can be undone (right away, or from Recent changes), which removes the copies that are still waiting. At most 200 are added at once.

## Sharing a collection or the whole library

Besides one model or one project, a **collection** can be shared (Collections tab, *share*) and so can the **whole library** (Settings, administrator only). The link opens a read-only gallery of names and pictures with a page per model; each page shows what a
single-model link shows (tags, designer, license, what worked, the original listing) and never notes, print history or costs. Downloads are only offered if you tick them for that link. Nothing on the page can run: it is plain HTML with a locked-down content policy,
and a link stops working when the collection is deleted, expires or is stopped. For people outside your network you still need to expose Model Hub yourself (a reverse proxy or VPN).

## Things Model Hub does by itself

One timer runs in the background (it wakes every half minute and runs whatever is due; a failing job never stops the others):

| What | How often | Settings |
|---|---|---|
| Saved backup (`auto-backup-*.zip` in `/config/backups`) | weekly by default | Settings → Backup: weekly / daily / off, and how many to keep (default 7). Other saved copies are never pruned by it |
| New uploads from designers you follow | every 6 hours | notification only; also in the Following tab |
| Low filament and supplies | every 6 hours | Settings → Notifications: warn at this many grams (default 100, 0 = off). Supplies use the minimum you set on each. You are told once per item, again only after it was restocked and ran low again |
| Listing changes | about daily, **off by default** | Settings → Notifications: check linked listings by itself |
| Printers | every 30 seconds | see below |
| Planned prints that the spools cannot cover | every 6 hours | notification only; Settings → Notifications has a switch |
| MQTT / Home Assistant | every 30 seconds, only if a broker is set | Settings → Home Assistant and MQTT |
| Cameras of printers you chose to watch | every 2 minutes, only while that printer is printing | printer card → Camera; needs a local vision model; notification only |
| Spools in a printer due for drying | daily | Filament → opened / dried; notification only |

**Printer finished:** when a printer that was printing stops, Model Hub notices (Klipper reports *complete*; for OctoPrint a print that ended at 100% counts as finished).
If the file was one Model Hub sent for a model, the print is recorded for that model: the waiting print-queue entry is completed (which takes the filament off the spool) or, with no
queue entry, a print-log entry with the print time is written. A cancelled or failed print is announced and kept in the print log as a failure (see Failed prints under [Print log](#print-log)). Nothing is ever started or stopped by this, and a print that ends while Model Hub is
restarting is not noticed (the state is kept in memory).

**Notifications** go to the webhook you set (ntfy, Discord, Slack...). **Settings → Notifications** has a *Send a test message* button and a switch for every kind: new files, AI tagging finished,
new uploads, listing changes, low stock, print finished or stopped, backup failed, a camera thinking a print failed, a spool due for drying.

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
