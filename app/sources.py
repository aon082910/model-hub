"""Match library models to their listings on model sites and pull in the
title, designer, license, description, tags and pictures.

Supported, no account needed: Printables (public GraphQL API), MakerWorld (the
JSON API behind api.bambulab.com; makerworld.com itself sits behind a bot
challenge that blocks server-side page fetches, the API host does not) and
Sketchfab (public search API). With a credential entered in Settings:
Thingiverse (access token), MyMiniFactory (API key) and Cults3D (nickname + API
key). All are read-only endpoints; the unofficial ones can change without
notice, so every failure here is turned into a SourceError with a message that
is fine to show to the user.

Three more keyless sources that also allow downloads: Wikimedia Commons (its 3D
files, found with the MediaWiki search API), NASA's public 3D Resources (a
GitHub repository, indexed from its file tree) and the Smithsonian's 3D
Digitization models that come as print-ready STL (its public 3D API).

Which sites let a server download the model files themselves: Printables (no
login), Thingiverse (with the token), Wikimedia Commons, NASA 3D Resources and the Smithsonian. MakerWorld, Sketchfab and MyMiniFactory
only hand files to a logged-in user, and Cults3D's API never serves files; for
those the browser extension, which runs in your logged-in browser, is the way in.

Only these fixed hosts are ever contacted, and pictures are only downloaded
from the sites' own CDNs, so a pasted URL can't make the server fetch anything
else on your network.
"""
import difflib
import hashlib
from concurrent.futures import ThreadPoolExecutor
import html
import io
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlparse

import logging

import httpx
from PIL import Image, ImageOps

from app.config import CONFIG_PATH

# httpx logs every request URL at INFO, and MyMiniFactory takes its API key in the
# query string, so keep that logger quiet rather than write keys into the log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

USER_AGENT = "ModelHub/2.1 (self-hosted model library; +https://github.com/aon082910/model-hub)"
TIMEOUT = httpx.Timeout(12.0, connect=5.0)

PRINTABLES_API = "https://api.printables.com/graphql/"
PRINTABLES_MEDIA = "https://media.printables.com/"
MAKERWORLD_API = "https://api.bambulab.com/v1"

SKETCHFAB_API = "https://api.sketchfab.com/v3"
THINGIVERSE_API = "https://api.thingiverse.com"

MYMINIFACTORY_API = "https://www.myminifactory.com/api/v2"
CULTS3D_API = "https://cults3d.com/graphql"

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
NASA_REPO = "nasa/NASA-3D-Resources"
NASA_TREE_API = f"https://api.github.com/repos/{NASA_REPO}/git/trees/master?recursive=1"
NASA_RAW = f"https://raw.githubusercontent.com/{NASA_REPO}/master/"
NASA_WEB = f"https://github.com/{NASA_REPO}/tree/master/"

PROVIDERS = ("printables", "makerworld", "sketchfab", "thingiverse", "myminifactory", "cults3d", "commons", "nasa3d", "smithsonian")
PROVIDER_LABELS = {
    "printables": "Printables", "makerworld": "MakerWorld", "sketchfab": "Sketchfab", "thingiverse": "Thingiverse",
    "myminifactory": "MyMiniFactory", "cults3d": "Cults3D",
    "commons": "Wikimedia Commons", "nasa3d": "NASA 3D Resources", "smithsonian": "Smithsonian 3D",
}
# Providers that need credentials: the fields to enter in Settings (each is stored
# as the setting "<provider>_<field>"), a label, whether it is secret, and where to get one.
CREDENTIAL_FIELDS = {
    "thingiverse": [("token", "Access token", True)],
    "myminifactory": [("key", "API key", True)],
    "cults3d": [("username", "Cults nickname", False), ("key", "API key", True)],
}
CREDENTIAL_HELP = {
    "thingiverse": "Create a free app at thingiverse.com/developers and paste its token.",
    "myminifactory": "Request an API key in your MyMiniFactory account's developer settings.",
    "cults3d": "Your Cults nickname, plus an API key created at cults3d.com/en/api/keys.",
}
KEYLESS_PROVIDERS = tuple(p for p in PROVIDERS if p not in CREDENTIAL_FIELDS)
# Where model files can be downloaded by this server (see the module docstring)
DOWNLOAD_PROVIDERS = ("printables", "thingiverse", "commons", "nasa3d", "smithsonian")
DOWNLOAD_NOTES = {
    "makerworld": "MakerWorld only gives model files to logged-in users. Open the listing, then use the Model Hub browser extension while logged in.",
    "sketchfab": "Sketchfab only gives downloads to logged-in users, and as glTF rather than printable formats.",
    "myminifactory": "MyMiniFactory only gives file downloads to logged-in users (the API key alone cannot). Open the listing and use the Model Hub browser extension while logged in.",
    "cults3d": "Cults3D never serves model files through its API. Open the listing and use the Model Hub browser extension while logged in.",
}
# Pictures are only ever downloaded from these sites' own domains (and their subdomains)
IMAGE_DOMAINS = (
    "printables.com", "bblmw.com", "sketchfab.com", "thingiverse.com", "myminifactory.com", "cults3d.com",
    "wikimedia.org", "githubusercontent.com", "si.edu",
)


def host_in_domains(host: Optional[str], domains) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def image_host_allowed(host: Optional[str]) -> bool:
    return host_in_domains(host, IMAGE_DOMAINS)


def credential_setting(provider: str, field: str) -> str:
    return f"{provider}_{field}"


def secret_setting_keys() -> set:
    return {credential_setting(p, f) for p, fields in CREDENTIAL_FIELDS.items() for f, _, secret in fields if secret}


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
_MYMINIFACTORY_URL = re.compile(r"^https?://(?:www\.)?myminifactory\.com/(?:[a-z]{2}/)?object/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)", re.I)
_CULTS3D_URL = re.compile(r"^https?://(?:www\.)?cults3d\.com/[a-z]{2}/3d-model/[^/?#]+/([A-Za-z0-9_-]{3,200})(?:[/?#]|$)", re.I)
_SMITHSONIAN_URL = re.compile(r"^https?://(?:www\.)?3d\.si\.edu/object/3d/(?:[^/?#]*:)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#]|$)", re.I)
_NASA_URL = re.compile(r"^https?://github\.com/nasa/NASA-3D-Resources/(?:tree|blob)/master/([^?#]+)", re.I)
_URL_PATTERNS = (
    ("printables", _PRINTABLES_URL), ("makerworld", _MAKERWORLD_URL),
    ("sketchfab", _SKETCHFAB_URL), ("thingiverse", _THINGIVERSE_URL),
    ("myminifactory", _MYMINIFACTORY_URL), ("cults3d", _CULTS3D_URL),
)


class SourceError(Exception):
    """A lookup failed in a way worth telling the user about."""


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False)


def nasa_listing_id(folder: str) -> str:
    return hashlib.sha1(folder.encode("utf-8")).hexdigest()[:12]


def parse_url(url: str) -> Optional[tuple]:
    """(provider, id) for a supported model URL, else None."""
    url = (url or "").strip()
    smithsonian = _SMITHSONIAN_URL.match(url)
    if smithsonian:
        return "smithsonian", smithsonian.group(1).lower()
    nasa = _NASA_URL.match(url)
    if nasa:
        path = unquote(nasa.group(1)).strip("/")
        if "." in path.rsplit("/", 1)[-1]:                 # a file inside a folder: the listing is the folder
            path = path.rsplit("/", 1)[0] if "/" in path else path
        return "nasa3d", nasa_listing_id(path) if path else None
    for provider, pattern in _URL_PATTERNS:
        match = pattern.match(url)
        if match:
            found = match.group(1)
            return provider, found.lower() if provider == "sketchfab" else found
    return None


def valid_source_id(provider: str, source_id) -> bool:
    source_id = str(source_id or "")
    if provider == "sketchfab":
        return re.fullmatch(r"[0-9a-f]{32}", source_id) is not None
    if provider == "cults3d":
        return re.fullmatch(r"[A-Za-z0-9_-]{3,200}", source_id) is not None
    if provider == "nasa3d":
        return re.fullmatch(r"[0-9a-f]{12}", source_id) is not None
    if provider == "smithsonian":
        return re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", source_id) is not None
    return provider in PROVIDERS and source_id.isdigit()


def load_credentials(session) -> dict:
    """{provider: {field: value}} for the providers whose credentials are all
    set in Settings (a provider with a field missing is left out)."""
    from app.settings_store import get_setting
    found = {}
    for provider, fields in CREDENTIAL_FIELDS.items():
        values = {f: (get_setting(session, credential_setting(provider, f)) or "").strip() for f, _, _ in fields}
        if all(values.values()):
            found[provider] = values
    return found


def _credential(credentials: Optional[dict], provider: str, field: str) -> str:
    return (((credentials or {}).get(provider) or {}).get(field) or "").strip()


def available_providers(credentials: Optional[dict] = None) -> tuple:
    """Providers to search by default: the keyless ones plus any with a credential."""
    def complete(provider):
        return all(_credential(credentials, provider, field) for field, _, _ in CREDENTIAL_FIELDS[provider])
    return tuple(p for p in PROVIDERS if p in KEYLESS_PROVIDERS or complete(p))


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
  user { id publicUsername }
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
        if provider == "myminifactory":
            return _myminifactory_details(client, source_id, _myminifactory_key(credentials))
        if provider == "cults3d":
            return _cults3d_details(client, source_id, _cults3d_auth(credentials))
        if provider == "commons":
            return _commons_details(client, source_id)
        if provider == "nasa3d":
            return _nasa_details(client, source_id)
        if provider == "smithsonian":
            return _smithsonian_details(client, source_id)
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
        "designer_handle": str((item.get("user") or {}).get("id") or ""),
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
        "designer_handle": user.get("username") or "",
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


def _search_sketchfab(client: httpx.Client, query: str, limit: int, page: int = 1) -> list:
    params = {"type": "models", "q": query, "count": limit, "downloadable": "true"}
    if page > 1:
        params["cursor"] = (page - 1) * limit         # their cursor is the offset into the results
    data = _sketchfab_get(client, "/search", **params)
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
            "designer_handle": user.get("username") or "",
            "license": license_info.get("label") or "",
            "thumbnail": _sketchfab_thumbnail((r.get("thumbnails") or {}).get("images"), 320),
        })
    return found


# ---------- Thingiverse (needs an access token from Settings) ----------

def _thingiverse_token(credentials: Optional[dict]) -> str:
    token = _credential(credentials, "thingiverse", "token")
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
        "designer_handle": (thing.get("creator") or {}).get("name") or "",
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


def _search_thingiverse(client: httpx.Client, query: str, limit: int, token: str, page: int = 1) -> list:
    data = _thingiverse_get(client, f"/search/{quote(query, safe='')}", token, type="things", per_page=limit, page=page)
    hits = data.get("hits") if isinstance(data, dict) else None
    return [{
        "provider": "thingiverse",
        "source_id": str(h["id"]),
        "url": h.get("public_url") or f"https://www.thingiverse.com/thing:{h['id']}",
        "title": h.get("name") or "",
        "designer": (h.get("creator") or {}).get("name") or "",
        "designer_handle": (h.get("creator") or {}).get("name") or "",
        "license": "",
        "thumbnail": h.get("thumbnail") or h.get("preview_image"),
    } for h in (hits or []) if h.get("id")]


# ---------- MyMiniFactory (API key; metadata only) ----------
# Their file download links need an OAuth-connected user, so the key alone only gets
# search, details and pictures.

def _myminifactory_key(credentials: Optional[dict]) -> str:
    key = _credential(credentials, "myminifactory", "key")
    if not key:
        raise SourceError("Add a MyMiniFactory API key in Settings to search MyMiniFactory")
    return key


def _myminifactory_get(client: httpx.Client, path: str, key: str, **params) -> dict:
    try:
        response = client.get(f"{MYMINIFACTORY_API}{path}", params={**params, "key": key})
    except httpx.HTTPError as e:
        raise SourceError(f"Could not reach MyMiniFactory ({e.__class__.__name__})")
    if response.status_code in (401, 403):
        raise SourceError("MyMiniFactory rejected the API key; check it in Settings")
    if response.status_code == 404:
        raise SourceError("That listing was not found (it may have been removed)")
    if response.status_code == 429:
        raise SourceError("MyMiniFactory is rate limiting requests; try again in a few minutes")
    if response.status_code != 200:
        raise SourceError(f"MyMiniFactory answered with an error ({response.status_code})")
    try:
        data = response.json()
    except ValueError:
        raise SourceError("MyMiniFactory returned something unexpected (it may have changed its API)")
    return data if isinstance(data, dict) else {}


def _mmf_image_url(image: dict, prefer: str) -> Optional[str]:
    for size in (prefer, "standard", "original", "thumbnail"):
        url = ((image or {}).get(size) or {}).get("url")
        if url:
            return url
    return None


def _mmf_license_text(licenses: list) -> str:
    labels = {"mention": "credit the designer", "remix": "remixing allowed",
              "commercial-use": "commercial use allowed", "exclusivity": "exclusive to MyMiniFactory"}
    allowed = [labels.get(l.get("type"), l.get("type")) for l in (licenses or []) if l.get("value") and l.get("type")]
    return ", ".join(allowed)


def _myminifactory_details(client: httpx.Client, source_id: str, key: str) -> dict:
    data = _myminifactory_get(client, f"/objects/{source_id}", key)
    if not data.get("id"):
        raise SourceError("That listing was not found (it may have been removed)")
    images = sorted(data.get("images") or [], key=lambda i: not i.get("is_primary"))
    urls = []
    for image in images:
        url = _mmf_image_url(image, "standard")
        if url and url not in urls:
            urls.append(url)
    designer = data.get("designer") or {}
    categories = data.get("categories") or []
    return {
        "provider": "myminifactory",
        "source_id": source_id,
        "url": data.get("url") or f"https://www.myminifactory.com/object/3d-print-{source_id}",
        "title": data.get("name") or "",
        "designer": designer.get("name") or designer.get("username") or "",
        "license": _mmf_license_text(data.get("licenses")),
        "description": html_to_text(data.get("description")),
        "tags": [t for t in (data.get("tags") or []) if isinstance(t, str) and t],
        "category": (categories[0].get("name") or "") if categories else "",
        "images": urls,
        "likes": data.get("likes"),
        "downloads": data.get("views"),
        "parts": [],
        "filaments": [],
    }


def _search_myminifactory(client: httpx.Client, query: str, limit: int, key: str, page: int = 1) -> list:
    data = _myminifactory_get(client, "/search", key, q=query, per_page=limit, page=page)
    found = []
    for item in data.get("items") or []:
        if not item.get("id"):
            continue
        images = sorted(item.get("images") or [], key=lambda i: not i.get("is_primary"))
        designer = item.get("designer") or {}
        found.append({
            "provider": "myminifactory",
            "source_id": str(item["id"]),
            "url": item.get("url") or f"https://www.myminifactory.com/object/3d-print-{item['id']}",
            "title": item.get("name") or "",
            "designer": designer.get("name") or designer.get("username") or "",
            "license": _mmf_license_text(item.get("licenses")),
            "thumbnail": _mmf_image_url(images[0], "thumbnail") if images else None,
        })
    return found


# ---------- Cults3D (nickname + API key; metadata only) ----------
# Their API deliberately never serves the 3D files. A listing's id here is its slug,
# the last part of its address.

def _cults3d_auth(credentials: Optional[dict]) -> tuple:
    username, key = _credential(credentials, "cults3d", "username"), _credential(credentials, "cults3d", "key")
    if not username or not key:
        raise SourceError("Add your Cults3D nickname and API key in Settings to search Cults3D")
    return username, key


def _cults3d_query(client: httpx.Client, query: str, variables: dict, auth: tuple) -> dict:
    try:
        response = client.post(CULTS3D_API, json={"query": query, "variables": variables}, auth=auth)
    except httpx.HTTPError as e:
        raise SourceError(f"Could not reach Cults3D ({e.__class__.__name__})")
    if response.status_code in (401, 403):
        raise SourceError("Cults3D rejected the nickname / API key; check them in Settings")
    if response.status_code == 429:
        raise SourceError("Cults3D is rate limiting requests; try again in a few minutes")
    if response.status_code != 200:
        raise SourceError(f"Cults3D answered with an error ({response.status_code})")
    try:
        payload = response.json()
    except ValueError:
        raise SourceError("Cults3D returned something unexpected (it may have changed its API)")
    if payload.get("errors"):
        message = (payload["errors"][0] or {}).get("message", "")
        raise SourceError(f"Cults3D rejected the request ({message[:80]})" if message else "Cults3D rejected the request")
    return payload.get("data") or {}


def _cults_slug(url: Optional[str]) -> str:
    return (urlparse(url or "").path.rstrip("/").rsplit("/", 1)[-1]) if url else ""


_CULTS_SEARCH = """
query($q: String!, $limit: Int!%s) { creationsSearchBatch(query: $q, limit: $limit%s) {
  total results { name(locale: EN) url(locale: EN) illustrationImageUrl creator { nick } license { name(locale: EN) } }
} }
"""
_CULTS_DETAIL = """
query($slug: String!) { creation(slug: $slug) {
  name(locale: EN) url(locale: EN) illustrationImageUrl %s
  license { name(locale: EN) } category { name(locale: EN) } tags(locale: EN)
  creator { nick } viewsCount likesCount downloadsCount
} }
"""


def _search_cults3d(client: httpx.Client, query: str, limit: int, auth: tuple, page: int = 1) -> list:
    variables = {"q": query, "limit": limit}
    if page > 1:
        variables["offset"] = (page - 1) * limit
        text = _CULTS_SEARCH % (", $offset: Int", ", offset: $offset")
    else:
        text = _CULTS_SEARCH % ("", "")
    data = _cults3d_query(client, text, variables, auth)
    found = []
    for item in ((data.get("creationsSearchBatch") or {}).get("results")) or []:
        slug = _cults_slug(item.get("url"))
        if not slug:
            continue
        found.append({
            "provider": "cults3d",
            "source_id": slug,
            "url": item.get("url"),
            "title": item.get("name") or "",
            "designer": (item.get("creator") or {}).get("nick") or "",
            "license": (item.get("license") or {}).get("name") or "",
            "thumbnail": item.get("illustrationImageUrl"),
        })
    return found


def _cults3d_details(client: httpx.Client, slug: str, auth: tuple) -> dict:
    try:
        data = _cults3d_query(client, _CULTS_DETAIL % "description(locale: EN)", {"slug": slug}, auth)
    except SourceError as e:
        if "description" not in str(e).lower():
            raise
        data = _cults3d_query(client, _CULTS_DETAIL % "", {"slug": slug}, auth)   # schema without a description field
    item = data.get("creation")
    if not item:
        raise SourceError("That listing was not found (it may have been removed)")
    cover = item.get("illustrationImageUrl")
    return {
        "provider": "cults3d",
        "source_id": slug,
        "url": item.get("url") or f"https://cults3d.com/en/3d-model/{slug}",
        "title": item.get("name") or "",
        "designer": (item.get("creator") or {}).get("nick") or "",
        "license": (item.get("license") or {}).get("name") or "",
        "description": html_to_text(item.get("description")),
        "tags": [t for t in (item.get("tags") or []) if isinstance(t, str) and t],
        "category": (item.get("category") or {}).get("name") or "",
        "images": [cover] if cover else [],
        "likes": item.get("likesCount"),
        "downloads": item.get("downloadsCount"),
        "parts": [],
        "filaments": [],
    }


def test_credentials(provider: str, credentials: Optional[dict]) -> str:
    """Make one small request with the stored credentials. Returns a message for
    the user on success; raises SourceError describing the problem otherwise."""
    if provider not in CREDENTIAL_FIELDS:
        raise SourceError(f"{PROVIDER_LABELS.get(provider, provider)} does not need credentials")
    found = search("benchy", providers=(provider,), limit=1, credentials=credentials)
    if found["errors"]:
        raise SourceError(found["errors"][provider])
    return f"{PROVIDER_LABELS[provider]} accepted the credentials."


# ---------- Wikimedia Commons (no account; its 3D files) ----------
# Each 3D file on Commons is its own listing: one model, with its license, author and a
# description page. Files download straight from upload.wikimedia.org.

_COMMONS_FIELDS = {
    "prop": "imageinfo|pageimages", "iiprop": "url|size|mime|extmetadata|mediatype", "iiurlwidth": 1200,
    "piprop": "thumbnail", "pithumbsize": 320,
}


def _commons_get(client: httpx.Client, **params) -> dict:
    data = _get_json(client, COMMONS_API, params={"action": "query", "format": "json", "formatversion": 2, **params})
    if data.get("error"):
        raise SourceError(f"Wikimedia Commons rejected the request ({(data['error'].get('code') or '')[:40]})")
    return data


def _commons_meta(info: dict, key: str) -> str:
    return html_to_text(((info.get("extmetadata") or {}).get(key) or {}).get("value") or "")


def _commons_author(text: str) -> str:
    """'CreativeTools: https://www.thingiverse.com/...' -> 'CreativeTools' (the link text is noise)."""
    cleaned = re.sub(r"https?://\S+", "", text or "").strip(" :-,;\n")
    return (cleaned or text or "")[:120]


def _commons_name(title: str) -> str:
    return re.sub(r"^File:", "", title or "").strip()


def _commons_entry(page: dict) -> Optional[dict]:
    info = (page.get("imageinfo") or [None])[0]
    if not info or Path(page.get("title", "")).suffix.lower() not in (".stl", ".obj", ".3mf", ".step", ".stp", ".fbx"):
        return None
    name = _commons_name(page["title"])
    return {
        "page": page, "info": info, "name": name,
        "title": Path(name).stem.replace("_", " "),
        "designer": _commons_author(_commons_meta(info, "Artist")),
        "license": _commons_meta(info, "LicenseShortName"),
    }


def _search_commons(client: httpx.Client, query: str, limit: int, page: int = 1) -> list:
    data = _commons_get(client, generator="search", gsrsearch=f"{query} filetype:3d", gsrnamespace=6,
                        gsrlimit=limit, gsroffset=(page - 1) * limit, **_COMMONS_FIELDS)
    pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 0))
    found = []
    for entry in filter(None, (_commons_entry(p) for p in pages)):
        found.append({
            "provider": "commons", "source_id": str(entry["page"]["pageid"]),
            "url": entry["info"].get("descriptionurl") or f"https://commons.wikimedia.org/?curid={entry['page']['pageid']}",
            "title": entry["title"], "designer": entry["designer"], "license": entry["license"],
            "thumbnail": (entry["page"].get("thumbnail") or {}).get("source"),
        })
    return found


def _commons_details(client: httpx.Client, source_id: str) -> dict:
    data = _commons_get(client, pageids=source_id, **{**_COMMONS_FIELDS, "prop": "imageinfo|pageimages|categories"},
                        cllimit=30, clshow="!hidden")
    pages = (data.get("query") or {}).get("pages") or []
    entry = _commons_entry(pages[0]) if pages and not pages[0].get("missing") else None
    if not entry:
        raise SourceError("That listing was not found (it may have been removed)")
    info, page = entry["info"], entry["page"]
    description = _commons_meta(info, "ImageDescription") or _commons_meta(info, "ObjectName")
    credit = _commons_meta(info, "Credit")
    tags = [re.sub(r"^Category:", "", c.get("title", "")).strip() for c in page.get("categories") or []]
    images = [u for u in (info.get("thumburl"), (page.get("thumbnail") or {}).get("source")) if u]
    return {
        "provider": "commons", "source_id": source_id,
        "url": info.get("descriptionurl") or f"https://commons.wikimedia.org/?curid={source_id}",
        "title": entry["title"], "designer": entry["designer"], "license": entry["license"],
        "description": "\n\n".join(x for x in (description, f"Source: {credit}" if credit else "") if x),
        "tags": [t for t in tags if t][:12], "category": "", "images": images[:1],
        "likes": None, "downloads": None, "parts": [], "filaments": [],
    }


def commons_files(client: httpx.Client, source_id: str) -> list:
    data = _commons_get(client, pageids=source_id, prop="imageinfo", iiprop="url|size")
    pages = (data.get("query") or {}).get("pages") or []
    info = (pages[0].get("imageinfo") or [None])[0] if pages else None
    if not info:
        raise SourceError("That listing was not found (it may have been removed)")
    return [{"id": str(source_id), "name": _commons_name(pages[0]["title"]), "size": info.get("size"), "url": info.get("url")}]


# ---------- NASA 3D Resources (no account; a public GitHub repository) ----------
# Every folder of the repo that holds a model file is one listing (97 at the time of
# writing: Apollo landing sites, satellites...). The repo's file tree is fetched once
# and kept for a few hours, so searching needs no further requests.

_NASA_TTL_SECONDS = 6 * 3600
_NASA_MODEL_EXTENSIONS = (".stl", ".3mf", ".obj", ".step", ".stp", ".fbx")
_NASA_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
_NASA_THUMBNAIL_MAX_BYTES = 600 * 1024        # larger pictures are not shown in result lists
_nasa_lock = threading.Lock()
_nasa_cache = {"at": 0.0, "folders": {}}


def reset_nasa_cache() -> None:
    with _nasa_lock:
        _nasa_cache.update(at=0.0, folders={})


def _nasa_folders(client: httpx.Client) -> dict:
    with _nasa_lock:
        if _nasa_cache["folders"] and time.time() - _nasa_cache["at"] < _NASA_TTL_SECONDS:
            return _nasa_cache["folders"]
    try:
        response = client.get(NASA_TREE_API, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as e:
        raise SourceError(f"Could not reach GitHub ({e.__class__.__name__})")
    if response.status_code in (403, 429):
        raise SourceError("GitHub is rate limiting requests; try again in a little while")
    if response.status_code != 200:
        raise SourceError(f"GitHub answered with an error ({response.status_code})")
    try:
        tree = response.json().get("tree") or []
    except ValueError:
        raise SourceError("GitHub returned something unexpected")
    folders = {}
    for entry in tree:
        if entry.get("type") != "blob" or "/" not in entry.get("path", ""):
            continue
        folder, filename = entry["path"].rsplit("/", 1)
        suffix = Path(filename).suffix.lower()
        kind = "models" if suffix in _NASA_MODEL_EXTENSIONS else "images" if suffix in _NASA_IMAGE_EXTENSIONS else None
        if kind:
            slot = folders.setdefault(nasa_listing_id(folder), {"folder": folder, "models": [], "images": []})
            slot[kind].append({"path": entry["path"], "name": filename, "size": entry.get("size") or 0})
    folders = {k: v for k, v in folders.items() if v["models"]}
    if not folders:
        raise SourceError("NASA's repository listing came back empty (it may have been reorganised)")
    with _nasa_lock:
        _nasa_cache.update(at=time.time(), folders=folders)
    return folders


def _nasa_raw(path: str) -> str:
    return NASA_RAW + quote(path)


def _nasa_title(folder: str) -> str:
    return folder.rsplit("/", 1)[-1]


def _search_nasa3d(client: httpx.Client, query: str, limit: int, page: int = 1) -> list:
    words = [w for w in re.split(r"\W+", query.lower()) if w]
    matches = [(i, f) for i, f in _nasa_folders(client).items() if all(w in f["folder"].lower() for w in words)]
    matches.sort(key=lambda m: m[1]["folder"].lower())
    found = []
    for source_id, f in matches[(page - 1) * limit: page * limit]:
        cover = next((i for i in f["images"] if i["size"] <= _NASA_THUMBNAIL_MAX_BYTES), None)
        found.append({
            "provider": "nasa3d", "source_id": source_id, "url": NASA_WEB + quote(f["folder"]),
            "title": _nasa_title(f["folder"]), "designer": "NASA", "license": "NASA public domain",
            "thumbnail": _nasa_raw(cover["path"]) if cover else None,
        })
    return found


def _nasa_folder(client: httpx.Client, source_id: str) -> dict:
    folder = _nasa_folders(client).get(source_id)
    if not folder:
        raise SourceError("That listing was not found (it may have been removed)")
    return folder


def _nasa_details(client: httpx.Client, source_id: str) -> dict:
    f = _nasa_folder(client, source_id)
    category = f["folder"].split("/")[0]
    files = ", ".join(m["name"] for m in f["models"])
    return {
        "provider": "nasa3d", "source_id": source_id, "url": NASA_WEB + quote(f["folder"]),
        "title": _nasa_title(f["folder"]), "designer": "NASA", "license": "NASA public domain",
        "description": f"From NASA's public 3D Resources collection ({category}).\nFiles: {files}",
        "tags": ["nasa", category.lower()], "category": category,
        "images": [_nasa_raw(i["path"]) for i in f["images"][:3]],
        "likes": None, "downloads": None, "parts": [], "filaments": [],
    }


def nasa_files(client: httpx.Client, source_id: str) -> list:
    f = _nasa_folder(client, source_id)
    return [{"id": hashlib.sha1(m["path"].encode("utf-8")).hexdigest()[:10], "name": m["name"], "size": m["size"],
             "url": _nasa_raw(m["path"])} for m in f["models"]]


# ---------- Smithsonian 3D (no account; the Smithsonian's public 3D API) ----------
# Only models the Smithsonian offers as print-ready STL (about a hundred: museum objects, fossils, corals, Apollo hardware)
# are searched, since the rest of its 3D collection is made for screens. A listing is one "3D package"; its files come
# from one query. Pictures and files are served from 3d-api.si.edu.

SMITHSONIAN_API = "https://3d-api.si.edu/api/v1.0/content/file/search"
SMITHSONIAN_CONTENT = "https://3d-api.si.edu/content/document/"
SMITHSONIAN_WEB = "https://3d.si.edu/object/3d/"
SMITHSONIAN_LICENSE = "Smithsonian (CC0 where the listing says so)"
SMITHSONIAN_DOMAINS = ("si.edu",)
_SI_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _si_web_url(title: str, source_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower().replace("'", "")).strip("-")[:80]
    return f"{SMITHSONIAN_WEB}{slug + ':' if slug else ''}{source_id}"


def _si_content_url(source_id: str, name: str) -> str:
    return f"{SMITHSONIAN_CONTENT}3d_package:{source_id}/{quote(name)}"


def _smithsonian_rows(client: httpx.Client, source_id: str) -> list:
    data = _get_json(client, SMITHSONIAN_API, params={"model_url": f"3d_package:{source_id}", "rows": 200})
    rows = [r for r in data.get("rows") or [] if isinstance(r, dict) and isinstance(r.get("content"), dict)]
    if not rows:
        raise SourceError("That listing was not found (it may have been removed)")
    return rows


def _search_smithsonian(client: httpx.Client, query: str, limit: int, page: int = 1) -> list:
    per_page = limit * 2                       # a model can come with more than one STL, so ask for more rows than results
    data = _get_json(client, SMITHSONIAN_API, params={"q": query, "model_type": "stl", "start": (page - 1) * per_page, "rows": per_page})
    seen, found = set(), []
    for row in data.get("rows") or []:
        content = (row.get("content") or {}) if isinstance(row, dict) else {}
        source_id = str(content.get("model_url") or "").removeprefix("3d_package:")
        if not _SI_ID.fullmatch(source_id) or source_id in seen:
            continue
        seen.add(source_id)
        title = (row.get("title") or "").strip() or "Smithsonian 3D model"
        found.append({
            "provider": "smithsonian", "source_id": source_id, "url": _si_web_url(title, source_id), "title": title,
            "designer": "Smithsonian Institution", "license": SMITHSONIAN_LICENSE,
            "thumbnail": _si_content_url(source_id, "scene-image-thumb.jpg"),
        })
        if len(found) >= limit:
            break
    return found


def _smithsonian_details(client: httpx.Client, source_id: str) -> dict:
    rows = _smithsonian_rows(client, source_id)
    title = next((r.get("title") for r in rows if r.get("title")), None) or "Smithsonian 3D model"
    pictures = {}
    for row in rows:
        content = row["content"]
        uri = str(content.get("uri") or "")
        if content.get("usage") == "Image2D" and host_in_domains(urlparse(uri).hostname, SMITHSONIAN_DOMAINS):
            pictures.setdefault(str(content.get("quality")), uri)
    images = [pictures[q] for q in ("Medium", "High", "Low") if q in pictures][:2]
    names = ", ".join(f["name"] for f in smithsonian_files_from_rows(rows, source_id) if f["print_ready"])
    return {
        "provider": "smithsonian", "source_id": source_id, "url": _si_web_url(title, source_id),
        "title": title, "designer": "Smithsonian Institution", "license": SMITHSONIAN_LICENSE,
        "description": ("From the Smithsonian's 3D Digitization program. Open the listing for the museum's own description of the object "
                        "and for how it may be used." + (f"\nPrint files: {names}" if names else "")),
        "tags": ["smithsonian", "museum"], "category": "Museum objects", "images": images,
        "likes": None, "downloads": None, "parts": [], "filaments": [],
    }


def smithsonian_files_from_rows(rows: list, source_id: str) -> list:
    """The model files a package offers: print-ready STLs, and the medium or low resolution OBJ downloads (the full resolution
    ones run to hundreds of MB). Each is {id, name, size, url, print_ready}."""
    out = []
    for row in rows:
        content = row.get("content") or {}
        uri = str(content.get("uri") or "")
        if content.get("usage") != "Download3D" or urlparse(uri).scheme != "https" or not host_in_domains(urlparse(uri).hostname, SMITHSONIAN_DOMAINS):
            continue
        model_type, quality = str(content.get("model_type") or "").lower(), str(content.get("quality") or "").lower()
        name = Path(unquote(urlparse(uri).path)).name
        stl = model_type == "stl"
        smaller_obj = model_type == "obj" and quality.startswith(("medium", "low"))
        if (stl or smaller_obj) and Path(name).suffix.lower() in (".zip", ".stl", ".obj"):
            out.append({"id": hashlib.sha1(uri.encode("utf-8")).hexdigest()[:10], "name": name, "size": content.get("file_size"),
                        "url": uri, "print_ready": stl})
    return out


def smithsonian_files(client: httpx.Client, source_id: str) -> list:
    return smithsonian_files_from_rows(_smithsonian_rows(client, source_id), source_id)


# ---------- model files (Printables, Thingiverse) ----------
# Only these two sites let a server fetch the files; see the module docstring.

_PRINTABLES_FILES = """
query($id: ID!) { print(id: $id) { id stls { id name fileSize } downloadPacks { id fileSize fileType } } }
"""
_PRINTABLES_LINK = """
mutation($id: ID!, $printId: ID!) {
  getDownloadLink(id: $id, printId: $printId, fileType: %s, source: model_detail) {
    ok errors { field messages } output { link }
  }
}
"""
_PRINTABLES_FILE_KINDS = {"stl", "pack"}      # the only values ever put into the query text


def printables_files(client: httpx.Client, source_id: str) -> list:
    """[{id, name, size, kind}] where kind is 'pack' (a zip of all the model
    files) or 'stl' (one file)."""
    item = _printables_query(client, _PRINTABLES_FILES, {"id": source_id}).get("print")
    if not item:
        raise SourceError("That listing was not found (it may have been removed)")
    files = []
    for pack in item.get("downloadPacks") or []:
        if pack.get("fileType") == "MODEL_FILES":
            files.append({"id": str(pack["id"]), "name": f"model-files-{source_id}.zip",
                          "size": pack.get("fileSize"), "kind": "pack"})
    for stl in item.get("stls") or []:
        files.append({"id": str(stl["id"]), "name": stl.get("name") or f"{stl['id']}.stl",
                      "size": stl.get("fileSize"), "kind": "stl"})
    return files


def printables_download_link(client: httpx.Client, source_id: str, file_id: str, kind: str) -> str:
    """A short-lived https link to one Printables file (no login needed)."""
    if kind not in _PRINTABLES_FILE_KINDS or not str(file_id).isdigit():
        raise SourceError("Unsupported file")
    data = _printables_query(client, _PRINTABLES_LINK % kind, {"id": str(file_id), "printId": str(source_id)})
    result = data.get("getDownloadLink") or {}
    link = (result.get("output") or {}).get("link")
    if not result.get("ok") or not link:
        raise SourceError("Printables did not give a download link for this file")
    parsed = urlparse(link)
    if parsed.scheme != "https" or not host_in_domains(parsed.hostname, ("printables.com",)):
        raise SourceError("Printables returned a download link on an unexpected site")
    return link


def thingiverse_files(client: httpx.Client, source_id: str, token: str) -> list:
    """[{id, name, size, url}] for a thing's files (url is the Thingiverse download address)."""
    data = _thingiverse_get(client, f"/things/{source_id}/files", token)
    if not isinstance(data, list):
        raise SourceError("Thingiverse returned something unexpected (it may have changed its API)")
    files = []
    for f in data:
        url = f.get("download_url") or f.get("url") or f.get("public_url")
        if f.get("name") and url:
            files.append({"id": str(f.get("id") or ""), "name": f["name"], "size": f.get("size"), "url": url})
    return files


def copy_images(from_model_id: int, to_model_id: int) -> list:
    """Give another model the same saved pictures (files in a multi-model pack share one listing)."""
    source = IMAGE_ROOT / str(from_model_id)
    if not source.is_dir():
        return []
    target = IMAGE_ROOT / str(to_model_id)
    target.mkdir(parents=True, exist_ok=True)
    names = []
    for picture in sorted(source.iterdir()):
        if picture.is_file() and re.fullmatch(r"\d{1,2}\.jpg", picture.name):
            (target / picture.name).write_bytes(picture.read_bytes())
            names.append(picture.name)
    return names


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
query($q: String!, $limit: Int!, $offset: Int!) { result: searchPrints2(query: $q, printType: print, limit: $limit, offset: $offset) {
  items { id name slug user { id publicUsername } license { abbreviation name } image { filePath } }
} }
"""


# Collections of reference models rather than designers' marketplaces: a personal file
# is unlikely to come from there, and a wrong automatic link is worse than none.
NOT_FOR_MATCHING = ("commons", "nasa3d", "smithsonian")


def matching_providers(credentials: Optional[dict] = None) -> tuple:
    """The sites the 'match my whole library' job searches."""
    return tuple(p for p in available_providers(credentials) if p not in NOT_FOR_MATCHING)


def search(query: str, providers=None, limit: int = 6, credentials: Optional[dict] = None, page: int = 1) -> dict:
    """{'results': [...], 'errors': {provider: message}}. One site being down
    does not hide the other's results. providers defaults to every site that can
    be searched right now (the keyless ones, plus any with credentials). page is
    1-based and applies to every site."""
    query = (query or "").strip()
    if not query:
        raise SourceError("Type something to search for")
    page = max(1, int(page or 1))
    providers = available_providers(credentials) if providers is None else providers
    wanted = [p for p in providers if p in PROVIDERS]

    def one(client, provider):
        if provider == "printables":
            return _search_printables(client, query, limit, page)
        if provider == "makerworld":
            return _search_makerworld(client, query, limit, page)
        if provider == "sketchfab":
            return _search_sketchfab(client, query, limit, page)
        if provider == "thingiverse":
            return _search_thingiverse(client, query, limit, _thingiverse_token(credentials), page)
        if provider == "myminifactory":
            return _search_myminifactory(client, query, limit, _myminifactory_key(credentials), page)
        if provider == "commons":
            return _search_commons(client, query, limit, page)
        if provider == "nasa3d":
            return _search_nasa3d(client, query, limit, page)
        if provider == "smithsonian":
            return _search_smithsonian(client, query, limit, page)
        return _search_cults3d(client, query, limit, _cults3d_auth(credentials), page)

    # every site at once: one slow site delays the answer, it does not add up
    results, errors = [], {}
    if wanted:
        with _client() as client, ThreadPoolExecutor(max_workers=len(wanted)) as pool:
            futures = {provider: pool.submit(one, client, provider) for provider in wanted}
            for provider in wanted:                 # in the sites' own order, so results stay stable
                try:
                    results.extend(futures[provider].result())
                except SourceError as e:
                    errors[provider] = str(e)
    return {"results": results, "errors": errors}


# ---------- a designer's newest uploads (for "follow") ----------
# Printables (verified against the live site) and Sketchfab (verified) need no account; Thingiverse
# uses its documented /users/<name>/things with your token.

FOLLOW_PROVIDERS = ("printables", "sketchfab", "thingiverse")
_PRINTABLES_USER_PRINTS = """
query($uid: ID!, $limit: Int!) { r: morePrints(userId: $uid, limit: $limit) {
  items { id name slug user { id publicUsername } license { abbreviation name } image { filePath } }
} }
"""
_HANDLE = re.compile(r"[A-Za-z0-9_.-]{1,60}")


def valid_designer_handle(provider: str, handle) -> bool:
    handle = str(handle or "")
    if provider == "printables":
        return handle.isdigit() and len(handle) <= 12
    return provider in FOLLOW_PROVIDERS and _HANDLE.fullmatch(handle) is not None


def designer_uploads(provider: str, handle: str, credentials: Optional[dict] = None, limit: int = 24) -> list:
    """The designer's newest listings, newest first, in the same shape as search results."""
    if not valid_designer_handle(provider, handle):
        raise SourceError("That designer cannot be followed")
    with _client() as client:
        if provider == "printables":
            data = _printables_query(client, _PRINTABLES_USER_PRINTS, {"uid": handle, "limit": limit})
            items = ((data.get("r") or {}).get("items")) or []
            return [{
                "provider": "printables", "source_id": str(i["id"]),
                "url": f"https://www.printables.com/model/{i['id']}-{i.get('slug') or ''}".rstrip("-"),
                "title": i.get("name") or "", "designer": (i.get("user") or {}).get("publicUsername") or "",
                "designer_handle": str((i.get("user") or {}).get("id") or handle),
                "license": (i.get("license") or {}).get("abbreviation") or (i.get("license") or {}).get("name") or "",
                "thumbnail": _printables_thumbnail((i.get("image") or {}).get("filePath")),
            } for i in items if i.get("id")]
        if provider == "sketchfab":
            data = _sketchfab_get(client, "/models", user=handle, count=limit, sort_by="-publishedAt")
            found = []
            for r in data.get("results") or []:
                if not r.get("uid"):
                    continue
                user, license_info = r.get("user") or {}, r.get("license") or {}
                found.append({
                    "provider": "sketchfab", "source_id": r["uid"],
                    "url": r.get("viewerUrl") or f"https://sketchfab.com/3d-models/{r['uid']}",
                    "title": r.get("name") or "", "designer": user.get("displayName") or user.get("username") or "",
                    "designer_handle": user.get("username") or handle, "license": license_info.get("label") or "",
                    "thumbnail": _sketchfab_thumbnail((r.get("thumbnails") or {}).get("images"), 320),
                })
            return found
        token = _thingiverse_token(credentials)
        data = _thingiverse_get(client, f"/users/{quote(handle, safe='')}/things", token, per_page=limit, page=1, sort="newest")
        return _thingiverse_things(data)


def _thingiverse_things(data) -> list:
    """Search-result-shaped entries from a Thingiverse list of things."""
    things = data.get("hits") if isinstance(data, dict) else data
    return [{
        "provider": "thingiverse", "source_id": str(t["id"]),
        "url": t.get("public_url") or f"https://www.thingiverse.com/thing:{t['id']}",
        "title": t.get("name") or "", "designer": (t.get("creator") or {}).get("name") or "",
        "designer_handle": (t.get("creator") or {}).get("name") or "", "license": "",
        "thumbnail": t.get("thumbnail") or t.get("preview_image"),
    } for t in (things or []) if isinstance(t, dict) and t.get("id")]


THINGIVERSE_COLLECTION = re.compile(r"^https?://(?:www\.)?thingiverse\.com/[^/]+/collections/(\d+)", re.I)


def thingiverse_import_list(kind: str, ref: str, credentials: Optional[dict], limit: int = 100) -> list:
    """Things from your likes (kind 'likes') or from a collection (kind 'collection', ref = id or link)."""
    token = _thingiverse_token(credentials)
    out = []
    with _client() as client:
        if kind == "likes":
            me = _thingiverse_get(client, "/users/me", token)
            name = me.get("name") if isinstance(me, dict) else None
            if not name or not _HANDLE.fullmatch(str(name)):
                raise SourceError("Thingiverse did not say whose token this is")
            path = f"/users/{quote(str(name), safe='')}/likes"
        elif kind == "collection":
            match = THINGIVERSE_COLLECTION.match(ref or "")
            collection_id = match.group(1) if match else (ref or "")
            if not str(collection_id).isdigit():
                raise SourceError("Give the collection's link (thingiverse.com/<user>/collections/<number>/...)")
            path = f"/collections/{collection_id}/things"
        else:
            raise SourceError("Unknown kind of list")
        page = 1
        while len(out) < limit:
            batch = _thingiverse_things(_thingiverse_get(client, path, token, per_page=30, page=page))
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 30:
                break
            page += 1
    return out[:limit]


def _search_printables(client: httpx.Client, query: str, limit: int, page: int = 1) -> list:
    data = _printables_query(client, _PRINTABLES_SEARCH, {"q": query, "limit": limit, "offset": (page - 1) * limit})
    items = ((data.get("result") or {}).get("items")) or []
    return [{
        "provider": "printables",
        "source_id": str(i["id"]),
        "url": f"https://www.printables.com/model/{i['id']}-{i.get('slug') or ''}".rstrip("-"),
        "title": i.get("name") or "",
        "designer": (i.get("user") or {}).get("publicUsername") or "",
        "designer_handle": str((i.get("user") or {}).get("id") or ""),
        "license": (i.get("license") or {}).get("abbreviation") or (i.get("license") or {}).get("name") or "",
        "thumbnail": _printables_thumbnail((i.get("image") or {}).get("filePath")),
    } for i in items]


def _search_makerworld(client: httpx.Client, query: str, limit: int, page: int = 1) -> list:
    data = _get_json(client, f"{MAKERWORLD_API}/search-service/select/design2",
                     params={"keyword": query, "limit": limit, "offset": (page - 1) * limit})
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
    if parsed.scheme != "https" or not image_host_allowed(parsed.hostname):
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
