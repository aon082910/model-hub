"""Match library models to their listings on model sites and pull in the
title, designer, license, description, tags and pictures.

Supported, no account needed: Printables (public GraphQL API), MakerWorld (the
JSON API behind api.bambulab.com; makerworld.com itself sits behind a bot
challenge that blocks server-side page fetches, the API host does not) and
Sketchfab (public search API). With an access token entered in Settings:
Thingiverse. All are read-only endpoints; the unofficial ones can change without
notice, so every failure here is turned into a SourceError with a message that
is fine to show to the user.

Only these fixed hosts are ever contacted, and pictures are only downloaded
from the sites' own CDNs, so a pasted URL can't make the server fetch anything
else on your network.
"""
import difflib
import html
import io
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
from PIL import Image, ImageOps

from app.config import CONFIG_PATH

USER_AGENT = "ModelHub/2.0 (self-hosted model library; +https://github.com/aon082910/model-hub)"
TIMEOUT = httpx.Timeout(12.0, connect=5.0)

PRINTABLES_API = "https://api.printables.com/graphql/"
PRINTABLES_MEDIA = "https://media.printables.com/"
MAKERWORLD_API = "https://api.bambulab.com/v1"

SKETCHFAB_API = "https://api.sketchfab.com/v3"
THINGIVERSE_API = "https://api.thingiverse.com"

PROVIDERS = ("printables", "makerworld", "sketchfab", "thingiverse")
PROVIDER_LABELS = {
    "printables": "Printables", "makerworld": "MakerWorld", "sketchfab": "Sketchfab", "thingiverse": "Thingiverse",
}
# providers that need a credential, and the Settings key it is stored under
CREDENTIAL_SETTINGS = {"thingiverse": "thingiverse_token"}
KEYLESS_PROVIDERS = tuple(p for p in PROVIDERS if p not in CREDENTIAL_SETTINGS)
IMAGE_HOSTS = {
    "media.printables.com", "makerworld.bblmw.com", "public-cdn.bblmw.com",
    "media.sketchfab.com", "cdn.thingiverse.com",
}

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 16 * 1024 * 1024   # largest download we will read
MAX_IMAGE_PIXELS = 25_000_000        # refused before decoding: a huge bitmap can need gigabytes of RAM
IMAGE_MAX_EDGE = 1200                # saved pictures are scaled to fit this, as JPEG
IMAGE_JPEG_QUALITY = 82
IMAGE_ROOT = CONFIG_PATH / "source_images"

_PRINTABLES_URL = re.compile(r"^https?://(?:www\.)?printables\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?model/(\d+)", re.I)
_MAKERWORLD_URL = re.compile(r"^https?://(?:www\.)?makerworld\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d+)", re.I)
_SKETCHFAB_URL = re.compile(r"^https?://(?:www\.)?sketchfab\.com/(?:3d-models|models)/(?:[^/?#]*-)?([0-9a-f]{32})(?:[/?#]|$)", re.I)
_THINGIVERSE_URL = re.compile(r"^https?://(?:www\.)?thingiverse\.com/thing:(\d+)", re.I)
_URL_PATTERNS = (
    ("printables", _PRINTABLES_URL), ("makerworld", _MAKERWORLD_URL),
    ("sketchfab", _SKETCHFAB_URL), ("thingiverse", _THINGIVERSE_URL),
)


class SourceError(Exception):
    """A lookup failed in a way worth telling the user about."""


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False)


def parse_url(url: str) -> Optional[tuple]:
    """(provider, id) for a supported model URL, else None."""
    url = (url or "").strip()
    for provider, pattern in _URL_PATTERNS:
        match = pattern.match(url)
        if match:
            return provider, match.group(1).lower()
    return None


def valid_source_id(provider: str, source_id) -> bool:
    source_id = str(source_id or "")
    if provider == "sketchfab":
        return re.fullmatch(r"[0-9a-f]{32}", source_id) is not None
    return provider in PROVIDERS and source_id.isdigit()


def load_credentials(session) -> dict:
    """{provider: secret} for the providers whose credential is set in Settings."""
    from app.settings_store import get_setting
    found = {}
    for provider, key in CREDENTIAL_SETTINGS.items():
        value = (get_setting(session, key) or "").strip()
        if value:
            found[provider] = value
    return found


def available_providers(credentials: Optional[dict] = None) -> tuple:
    """Providers to search by default: the keyless ones plus any with a credential."""
    credentials = credentials or {}
    return tuple(p for p in PROVIDERS if p in KEYLESS_PROVIDERS or credentials.get(p))


def html_to_text(value: Optional[str]) -> str:
    """Listing descriptions are HTML; keep the text and its paragraph breaks."""
    if not value:
        return ""
    text = re.sub(r"(?i)<\s*br\s*/?>|</\s*(p|div|h[1-6]|ul|ol|li|tr)\s*>", "\n", value)
    text = re.sub(r"(?i)<\s*li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _get_json(client: httpx.Client, url: str, **kwargs) -> dict:
    try:
        response = client.get(url, **kwargs)
    except httpx.HTTPError as e:
        raise SourceError(f"Could not reach the site ({e.__class__.__name__})")
    if response.status_code == 404:
        raise SourceError("That listing was not found (it may have been removed)")
    if response.status_code == 429:
        raise SourceError("The site is rate limiting requests; try again in a few minutes")
    if response.status_code != 200:
        raise SourceError(f"The site answered with an error ({response.status_code})")
    try:
        return response.json()
    except ValueError:
        raise SourceError("The site returned something unexpected (it may have changed its API)")


def _printables_query(client: httpx.Client, query: str, variables: Optional[dict] = None) -> dict:
    try:
        response = client.post(PRINTABLES_API, json={"query": query, "variables": variables or {}})
    except httpx.HTTPError as e:
        raise SourceError(f"Could not reach Printables ({e.__class__.__name__})")
    if response.status_code == 429:
        raise SourceError("Printables is rate limiting requests; try again in a few minutes")
    if response.status_code != 200:
        raise SourceError(f"Printables answered with an error ({response.status_code})")
    try:
        payload = response.json()
    except ValueError:
        raise SourceError("Printables returned something unexpected")
    if payload.get("errors") or payload.get("data") is None:
        raise SourceError("Printables rejected the request (it may have changed its API)")
    return payload["data"]


def _printables_media(file_path: Optional[str]) -> Optional[str]:
    return PRINTABLES_MEDIA + file_path.lstrip("/") if file_path else None


def _printables_thumbnail(file_path: Optional[str]) -> Optional[str]:
    """A 320px version of a Printables picture for result lists (a few KB
    instead of several MB). Their image host resizes when the path carries
    thumbs/inside/<size>/<format>/ and the format matches the file's extension;
    anything else falls back to the original."""
    if not file_path:
        return None
    folder, _, name = file_path.lstrip("/").rpartition("/")
    extension = name.rpartition(".")[2].lower()
    if folder and extension in ("jpg", "png", "webp", "gif"):
        return f"{PRINTABLES_MEDIA}{folder}/thumbs/inside/320x320/{extension}/{name}"
    return _printables_media(file_path)


# ---------- details ----------

_PRINTABLES_DETAIL = """
query($id: ID!) { print(id: $id) {
  id name slug summary description
  license { name abbreviation }
  user { publicUsername }
  image { filePath }
  images { filePath }
  category { name }
  tags { name }
  likesCount downloadCount
} }
"""


def fetch_details(provider: str, source_id: str, credentials: Optional[dict] = None) -> dict:
    """Normalised details for one listing; raises SourceError."""
    if not valid_source_id(provider, source_id):
        raise SourceError("Unsupported listing")
    source_id = str(source_id)
    with _client() as client:
        if provider == "printables":
            return _printables_details(client, source_id)
        if provider == "makerworld":
            return _makerworld_details(client, source_id)
        if provider == "sketchfab":
            return _sketchfab_details(client, source_id)
        return _thingiverse_details(client, source_id, _thingiverse_token(credentials))


def _printables_details(client: httpx.Client, source_id: str) -> dict:
    item = _printables_query(client, _PRINTABLES_DETAIL, {"id": source_id}).get("print")
    if not item:
        raise SourceError("That listing was not found (it may have been removed)")
    images = []
    for entry in [item.get("image")] + (item.get("images") or []):
        url = _printables_media((entry or {}).get("filePath"))
        if url and url not in images:
            images.append(url)
    license_info = item.get("license") or {}
    return {
        "provider": "printables",
        "source_id": source_id,
        "url": f"https://www.printables.com/model/{source_id}-{item.get('slug') or ''}".rstrip("-"),
        "title": item.get("name") or "",
        "designer": (item.get("user") or {}).get("publicUsername") or "",
        "license": license_info.get("abbreviation") or license_info.get("name") or "",
        "description": html_to_text(item.get("description")) or html_to_text(item.get("summary")),
        "tags": [t["name"] for t in (item.get("tags") or []) if t.get("name")],
        "category": (item.get("category") or {}).get("name") or "",
        "images": images,
        "likes": item.get("likesCount"),
        "downloads": item.get("downloadCount"),
        "parts": [],
        "filaments": [],
    }


def _makerworld_details(client: httpx.Client, source_id: str) -> dict:
    data = _get_json(client, f"{MAKERWORLD_API}/design-service/design/{source_id}")
    if not data.get("id"):
        raise SourceError("That listing was not found (it may have been removed)")
    extension = data.get("designExtension") or {}
    images = []
    for entry in (extension.get("design_pictures") or []) + (extension.get("real_pictures") or []):
        url = (entry or {}).get("url")
        if url and url not in images:
            images.append(url)
    cover = data.get("coverUrl")
    if cover and cover not in images:
        images.insert(0, cover)
    categories = data.get("categories") or []
    return {
        "provider": "makerworld",
        "source_id": source_id,
        "url": f"https://makerworld.com/en/models/{source_id}-{data.get('slug') or ''}".rstrip("-"),
        "title": data.get("title") or "",
        "designer": (data.get("designCreator") or {}).get("name") or "",
        "license": data.get("license") or "",
        "description": html_to_text(data.get("summary")),
        "tags": [t for t in (data.get("tags") or []) if t],
        "category": categories[0].get("name", "") if categories else "",
        "images": images,
        "likes": data.get("likeCount"),
        "downloads": data.get("downloadCount"),
        "parts": makerworld_parts(extension),
        "filaments": makerworld_filaments(extension),
    }


# ---------- Sketchfab (no account needed) ----------

def _sketchfab_get(client: httpx.Client, path: str, **params) -> dict:
    return _get_json(client, f"{SKETCHFAB_API}{path}", params=params or None)


def _sketchfab_thumbnail(images: list, target_width: int) -> Optional[str]:
    candidates = [i for i in images or [] if i.get("url") and i.get("width")]
    if not candidates:
        return None
    return min(candidates, key=lambda i: abs(i["width"] - target_width))["url"]


def _sketchfab_details(client: httpx.Client, source_id: str) -> dict:
    data = _sketchfab_get(client, f"/models/{source_id}")
    if not data.get("uid"):
        raise SourceError("That listing was not found (it may have been removed)")
    images = (data.get("thumbnails") or {}).get("images") or []
    cover = _sketchfab_thumbnail(images, 1600)
    categories = data.get("categories") or []
    user = data.get("user") or {}
    license_info = data.get("license") or {}
    return {
        "provider": "sketchfab",
        "source_id": source_id,
        "url": data.get("viewerUrl") or f"https://sketchfab.com/3d-models/{source_id}",
        "title": data.get("name") or "",
        "designer": user.get("displayName") or user.get("username") or "",
        "license": license_info.get("label") or license_info.get("fullName") or "",
        "description": html_to_text(data.get("description")),
        "tags": [t["name"] for t in (data.get("tags") or []) if t.get("name")],
        "category": categories[0].get("name", "") if categories else "",
        "images": [cover] if cover else [],
        "likes": data.get("likeCount"),
        "downloads": data.get("downloadCount"),
        "parts": [],
        "filaments": [],
    }


def _search_sketchfab(client: httpx.Client, query: str, limit: int) -> list:
    data = _sketchfab_get(client, "/search", type="models", q=query, count=limit, downloadable="true")
    found = []
    for r in data.get("results") or []:
        if not r.get("uid"):
            continue
        user = r.get("user") or {}
        license_info = r.get("license") or {}
        found.append({
            "provider": "sketchfab",
            "source_id": r["uid"],
            "url": r.get("viewerUrl") or f"https://sketchfab.com/3d-models/{r['uid']}",
            "title": r.get("name") or "",
            "designer": user.get("displayName") or user.get("username") or "",
            "license": license_info.get("label") or "",
            "thumbnail": _sketchfab_thumbnail((r.get("thumbnails") or {}).get("images"), 320),
        })
    return found


# ---------- Thingiverse (needs an access token from Settings) ----------

def _thingiverse_token(credentials: Optional[dict]) -> str:
    token = ((credentials or {}).get("thingiverse") or "").strip()
    if not token:
        raise SourceError("Add a Thingiverse access token in Settings to search Thingiverse")
    return token


def _thingiverse_get(client: httpx.Client, path: str, token: str, **params):
    """GET a Thingiverse API path. The token goes in a header, never the URL,
    so it can't end up in a log or an error message."""
    try:
        response = client.get(f"{THINGIVERSE_API}{path}", params=params or None,
                              headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise SourceError(f"Could not reach Thingiverse ({e.__class__.__name__})")
    if response.status_code in (401, 403):
        raise SourceError("Thingiverse rejected the access token; check it in Settings")
    if response.status_code == 404:
        raise SourceError("That listing was not found (it may have been removed)")
    if response.status_code == 429:
        raise SourceError("Thingiverse is rate limiting requests; try again in a few minutes")
    if response.status_code != 200:
        raise SourceError(f"Thingiverse answered with an error ({response.status_code})")
    try:
        return response.json()
    except ValueError:
        raise SourceError("Thingiverse returned something unexpected (it may have changed its API)")


def _thingiverse_image_url(image: dict) -> Optional[str]:
    for size in image.get("sizes") or []:
        if size.get("type") == "display" and size.get("size") == "large" and size.get("url"):
            return size["url"]
    return image.get("url")


def _thingiverse_details(client: httpx.Client, source_id: str, token: str) -> dict:
    thing = _thingiverse_get(client, f"/things/{source_id}", token)
    if not isinstance(thing, dict) or not thing.get("id"):
        raise SourceError("That listing was not found (it may have been removed)")
    images = []
    for image in _thingiverse_get(client, f"/things/{source_id}/images", token) or []:
        url = _thingiverse_image_url(image)
        if url and url not in images:
            images.append(url)
    if not images and thing.get("thumbnail"):
        images = [thing["thumbnail"]]
    tags = [t["name"] for t in (_thingiverse_get(client, f"/things/{source_id}/tags", token) or []) if t.get("name")]
    description = html_to_text(thing.get("description_html")) or html_to_text(thing.get("description"))
    instructions = html_to_text(thing.get("instructions_html")) or html_to_text(thing.get("instructions"))
    return {
        "provider": "thingiverse",
        "source_id": source_id,
        "url": thing.get("public_url") or f"https://www.thingiverse.com/thing:{source_id}",
        "title": thing.get("name") or "",
        "designer": (thing.get("creator") or {}).get("name") or "",
        "license": thing.get("license") or "",
        "description": "\n\n".join(part for part in (description, instructions) if part),
        "tags": tags,
        "category": "",
        "images": images,
        "likes": thing.get("like_count"),
        "downloads": thing.get("download_count"),
        "parts": [],
        "filaments": [],
    }


def _search_thingiverse(client: httpx.Client, query: str, limit: int, token: str) -> list:
    data = _thingiverse_get(client, f"/search/{quote(query, safe='')}", token, type="things", per_page=limit)
    hits = data.get("hits") if isinstance(data, dict) else None
    return [{
        "provider": "thingiverse",
        "source_id": str(h["id"]),
        "url": h.get("public_url") or f"https://www.thingiverse.com/thing:{h['id']}",
        "title": h.get("name") or "",
        "designer": (h.get("creator") or {}).get("name") or "",
        "license": "",
        "thumbnail": h.get("thumbnail") or h.get("preview_image"),
    } for h in (hits or []) if h.get("id")]


# ---------- parts lists (MakerWorld "what you need to buy") ----------

_SUPPLY_WORDS = re.compile(
    r"glue|adhesive|\btape\b|solder|flux|paint|primer|sandpaper|zip ?ties?|cable ?ties?|epoxy|lubricant|"
    r"grease|cleaning|alcohol|velcro|heat ?shrink|thread ?locker|loctite", re.I)
_ELECTRONIC_WORDS = re.compile(
    r"servo|arduino|esp[ -]?\d|raspberry|\bpi\b|pico|\bleds?\b|neopixel|sensor|display|oled|lcd|\bboard\b|"
    r"controller|batter|motor|switch|button|wires?\b|cables?\b|jumper|resistor|capacitor|usb|module|relay|"
    r"speaker|buzzer|microcontroller|breadboard|pcb|chip|transistor|diode|\bfans?\b|stepper|driver|camera|"
    r"antenna|charger|power supply|psu|tea ?light|candle|\blights?\b|\blamps?\b|cyberbrick|bluetooth|wifi", re.I)
_HARDWARE_WORDS = re.compile(
    r"screws?|bolts?|\bnuts?\b|washers?|inserts?|bearings?|standoffs?|spacers?|\brods?\b|springs?|magnets?|"
    r"hinges?|brackets?|rivets?|\bbhcs\b|\bshcs\b|\bpins?\b", re.I)
_UNIT_AFTER_NUMBER = re.compile(r"^(mm|cm|m|v|a|mah|k|uf|ohm|gb|mb|in|inch|pin|pcs)\b", re.I)


def classify_part(name: str) -> str:
    """Best guess at electronics / parts / supplies from a part's name. It is
    only a default -- every imported row can be changed before it is added."""
    if _SUPPLY_WORDS.search(name):
        return "supplies"
    if _HARDWARE_WORDS.search(name):          # before electronics: "Button Head" screws, "LED standoffs"
        return "parts"
    if _ELECTRONIC_WORDS.search(name):
        return "electronics"
    return "parts"


def _store_part_name(item: dict) -> str:
    parent = (item.get("displayParentTitle") or item.get("parentTitle") or "").strip()
    title = (item.get("displayTitle") or item.get("title") or "").strip()
    if not parent or parent.lower() in title.lower():
        return title or parent
    return f"{parent} - {title}" if title else parent


def parse_free_parts(text: str) -> list:
    """A listing's plain-text parts ('- 2x MG90S servo' per line) as rows."""
    rows = []
    for line in re.split(r"[\r\n]+", text or ""):
        line = re.sub(r"^[\s\-\*\u2022]+", "", line)
        line = re.sub(r"^\d{1,2}[.)]\s+", "", line).strip()      # "1. DeskPi board" list numbering
        if not line:
            continue
        quantity, name = 1, line
        lead = re.match(r"^(\d{1,3})\s*[x\u00d7]?\s+(.+)$", line)
        trail = re.match(r"^(.+?)\s*[x\u00d7]\s*(\d{1,3})$", line)
        if lead and not _UNIT_AFTER_NUMBER.match(lead.group(2)):
            quantity, name = int(lead.group(1)), lead.group(2).strip()
        elif trail:
            quantity, name = int(trail.group(2)), trail.group(1).strip()
        rows.append({"name": name[:200], "quantity": max(1, quantity), "unit_cost": None,
                     "purchase_url": None, "notes": None})
    return rows


def makerworld_parts(extension: dict) -> list:
    """Every non-filament part a MakerWorld listing says you need: Bambu Lab
    store items (with link and price) and the designer's own free-text list.
    Rows are {name, category, quantity, unit_cost, purchase_url, notes, kind}."""
    prices = {}
    for spu in extension.get("boms_v2") or []:
        for sku in spu.get("productSkuList") or []:
            if sku.get("sku") and sku.get("price"):
                prices[str(sku["sku"]).lower()] = (sku["price"], sku.get("currency") or "")

    rows = []
    for item in (extension.get("boms") or []) + (extension.get("boms_of_materials") or []):
        name = _store_part_name(item)
        if not name:
            continue
        price, currency = prices.get(str(item.get("sku") or "").lower(), (None, ""))
        if price is None and (item.get("priceInfo") or {}).get("priceX100"):
            price, currency = item["priceInfo"]["priceX100"] / 100, item["priceInfo"].get("code") or ""
        link = item.get("url") or ""
        quantity = max(1, int(item.get("quantity") or 1))
        note = "From the listing's Bambu Lab store list"
        if re.search(r"\(\d+\s*pcs\)", name, re.I):
            # the listing's "x11" and the pack size in the name don't say how many packs to buy
            note += f"; listing quantity x{quantity}, sold in packs - set how many you need"
        if currency and currency != "USD":
            note += f" (price in {currency})"
        rows.append({
            "name": name[:200], "quantity": quantity, "unit_cost": price,
            "purchase_url": link if link.startswith("https://") else None,
            "notes": note, "kind": "store",
        })
    for entry in extension.get("boms_of_other_part_list") or []:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        note = (entry.get("note") or "").strip()[:200] or "From the listing's parts list"
        if "\n" in name:
            # a designer pasted a whole list into one entry: split it into rows
            for row in parse_free_parts(name):
                rows.append({**row, "notes": note, "kind": "listed"})
            continue
        rows.append({
            "name": name[:200], "quantity": max(1, int(entry.get("quantity") or 1)), "unit_cost": None,
            "purchase_url": None, "notes": note, "kind": "listed",
        })
    free = extension.get("boms_of_other_parts")
    if isinstance(free, str):
        for row in parse_free_parts(free):
            rows.append({**row, "notes": "From the listing's parts list", "kind": "listed"})

    unique, seen = [], set()
    for row in rows:                       # the same part is often listed twice (structured list and free text)
        key = row["name"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        row["category"] = classify_part(row["name"])
        unique.append(row)
    return unique


# ---------- filament a listing suggests (MakerWorld) ----------

_COLOR_PAREN = re.compile(r"\s*\(([^)]*)\).*$")


def makerworld_filaments(extension: dict) -> list:
    """The filament a MakerWorld listing recommends, e.g. 'PLA Basic' in 'Gray
    (10103) / Refill / 1kg', as {material, brand, color, code, label}."""
    found, seen = [], set()
    for item in extension.get("boms_of_filaments") or []:
        parent = (item.get("displayParentTitle") or item.get("parentTitle") or "").strip()
        title = (item.get("displayTitle") or item.get("title") or "").strip()
        if not parent:
            continue
        material = parent.split()[0].upper()
        color_text = title.split("/")[0].strip()
        code = None
        paren = re.search(r"\(([^)]*)\)", color_text)
        if paren and paren.group(1).strip().isdigit():
            code = paren.group(1).strip()
        color = _COLOR_PAREN.sub("", color_text).strip()
        key = (material, color.lower())
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "material": material, "brand": "Bambu Lab", "color": color, "code": code,
            "label": f"{parent} {color}".strip(),
        })
    return found


# ---------- search ----------

_PRINTABLES_SEARCH = """
query($q: String!, $limit: Int!) { result: searchPrints2(query: $q, printType: print, limit: $limit) {
  items { id name slug user { publicUsername } license { abbreviation name } image { filePath } }
} }
"""


def search(query: str, providers=None, limit: int = 6, credentials: Optional[dict] = None) -> dict:
    """{'results': [...], 'errors': {provider: message}}. One site being down
    does not hide the other's results. providers defaults to every site that can
    be searched right now (the keyless ones, plus any with a credential)."""
    query = (query or "").strip()
    if not query:
        raise SourceError("Type something to search for")
    providers = available_providers(credentials) if providers is None else providers
    results, errors = [], {}
    with _client() as client:
        for provider in providers:
            if provider not in PROVIDERS:
                continue
            try:
                if provider == "printables":
                    found = _search_printables(client, query, limit)
                elif provider == "makerworld":
                    found = _search_makerworld(client, query, limit)
                elif provider == "sketchfab":
                    found = _search_sketchfab(client, query, limit)
                else:
                    found = _search_thingiverse(client, query, limit, _thingiverse_token(credentials))
                results.extend(found)
            except SourceError as e:
                errors[provider] = str(e)
    return {"results": results, "errors": errors}


def _search_printables(client: httpx.Client, query: str, limit: int) -> list:
    data = _printables_query(client, _PRINTABLES_SEARCH, {"q": query, "limit": limit})
    items = ((data.get("result") or {}).get("items")) or []
    return [{
        "provider": "printables",
        "source_id": str(i["id"]),
        "url": f"https://www.printables.com/model/{i['id']}-{i.get('slug') or ''}".rstrip("-"),
        "title": i.get("name") or "",
        "designer": (i.get("user") or {}).get("publicUsername") or "",
        "license": (i.get("license") or {}).get("abbreviation") or (i.get("license") or {}).get("name") or "",
        "thumbnail": _printables_thumbnail((i.get("image") or {}).get("filePath")),
    } for i in items]


def _search_makerworld(client: httpx.Client, query: str, limit: int) -> list:
    data = _get_json(client, f"{MAKERWORLD_API}/search-service/select/design2",
                     params={"keyword": query, "limit": limit, "offset": 0})
    return [{
        "provider": "makerworld",
        "source_id": str(h["id"]),
        "url": f"https://makerworld.com/en/models/{h['id']}-{h.get('slug') or ''}".rstrip("-"),
        "title": h.get("title") or "",
        "designer": (h.get("designCreator") or {}).get("name") or "",
        "license": h.get("license") or "",
        "thumbnail": h.get("cover"),
    } for h in (data.get("hits") or []) if h.get("id")]


# ---------- suggesting a match for a library file ----------

_NOISE_WORDS = {"stl", "3mf", "obj", "step", "stp", "fbx", "model", "models", "print", "fixed", "final",
                "copy", "plate", "file", "files", "v1", "v2", "v3", "supports", "support", "printable"}


def suggest_query(filename: str) -> str:
    """Turn 'Benchy_v2-fixed (1).stl' into 'benchy' -- a decent search string."""
    stem = Path(filename).stem
    stem = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", stem)
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    stem = re.sub(r"[_\-.+]+", " ", stem)
    words = [w for w in stem.split() if w.lower() not in _NOISE_WORDS and not re.fullmatch(r"\d{5,}|[0-9a-f]{8,}", w, re.I)]
    return " ".join(words).strip()


def score_title(query: str, title: str) -> float:
    """0..1 similarity between the search string and a listing title."""
    a, b = query.lower().strip(), title.lower().strip()
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    query_words = set(re.findall(r"\w+", a))
    title_words = set(re.findall(r"\w+", b))
    overlap = len(query_words & title_words) / len(query_words) if query_words else 0.0
    return round(max(ratio, 0.5 * ratio + 0.5 * overlap), 3)


def rank(results: list, query: str) -> list:
    for r in results:
        r["score"] = score_title(query, r["title"])
    return sorted(results, key=lambda r: r["score"], reverse=True)


# ---------- pictures ----------

def _image_extension(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def download_image(client: httpx.Client, url: str) -> tuple:
    """(bytes, extension). The type comes from the file's own header, not the URL."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
        raise SourceError("Picture is not on a supported site")
    chunks, size = [], 0
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise SourceError(f"Picture download failed ({response.status_code})")
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise SourceError("Picture is too large")
                chunks.append(chunk)
    except httpx.HTTPError as e:
        raise SourceError(f"Picture download failed ({e.__class__.__name__})")
    data = b"".join(chunks)
    extension = _image_extension(data)
    if not extension:
        raise SourceError("Picture is not a supported image type")
    return data, extension


def shrink_image(data: bytes) -> bytes:
    """Re-encode a downloaded picture as a modest JPEG. Listing photos are often
    several MB each; the library only needs a preview-sized copy of them."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise SourceError("Picture is too large")
            img.draft("RGB", (IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))   # JPEG: decode at reduced size
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, (236, 238, 241))   # transparent areas on light grey
                flat.paste(img, mask=img.getchannel("A"))
                img = flat
            else:
                img = img.convert("RGB")
            img.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
            return out.getvalue()
    except SourceError:
        raise
    except Exception:
        raise SourceError("Picture could not be read")


def store_images(model_id: int, urls: list) -> list:
    """Replace the model's saved pictures with these (best effort: a picture
    that fails is skipped). Returns the stored file names."""
    folder = IMAGE_ROOT / str(model_id)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if old.is_file():
            old.unlink()
    names = []
    with _client() as client:
        for url in urls[:MAX_IMAGES]:
            try:
                data, _ = download_image(client, url)
                data = shrink_image(data)
            except SourceError:
                continue
            name = f"{len(names) + 1}.jpg"
            (folder / name).write_bytes(data)
            names.append(name)
    return names


def image_path(model_id: int, name: str) -> Optional[Path]:
    """Path of a stored picture, or None (also for any name that isn't a plain file name)."""
    if not re.fullmatch(r"\d{1,2}\.(jpg|png|webp|gif)", name):
        return None
    path = IMAGE_ROOT / str(model_id) / name
    return path if path.is_file() else None


def delete_images(model_id: int) -> None:
    folder = IMAGE_ROOT / str(model_id)
    if folder.is_dir():
        for old in folder.iterdir():
            if old.is_file():
                old.unlink()
        folder.rmdir()
