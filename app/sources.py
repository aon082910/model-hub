"""Match library models to their listings on model sites and pull in the
title, designer, license, description, tags and pictures.

Supported: Printables (public GraphQL API) and MakerWorld (the JSON API behind
api.bambulab.com; makerworld.com itself sits behind a bot challenge that blocks
server-side page fetches, the API host does not). Both are unofficial, public,
read-only endpoints, so they can change without notice -- every failure here is
turned into a SourceError with a message that is fine to show to the user.

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
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps

from app.config import CONFIG_PATH

USER_AGENT = "ModelHub/1.8 (self-hosted model library; +https://github.com/aon082910/model-hub)"
TIMEOUT = httpx.Timeout(12.0, connect=5.0)

PRINTABLES_API = "https://api.printables.com/graphql/"
PRINTABLES_MEDIA = "https://media.printables.com/"
MAKERWORLD_API = "https://api.bambulab.com/v1"

PROVIDERS = ("printables", "makerworld")
PROVIDER_LABELS = {"printables": "Printables", "makerworld": "MakerWorld"}
IMAGE_HOSTS = {"media.printables.com", "makerworld.bblmw.com", "public-cdn.bblmw.com"}

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 16 * 1024 * 1024   # largest download we will read
MAX_IMAGE_PIXELS = 25_000_000        # refused before decoding: a huge bitmap can need gigabytes of RAM
IMAGE_MAX_EDGE = 1200                # saved pictures are scaled to fit this, as JPEG
IMAGE_JPEG_QUALITY = 82
IMAGE_ROOT = CONFIG_PATH / "source_images"

_PRINTABLES_URL = re.compile(r"^https?://(?:www\.)?printables\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?model/(\d+)", re.I)
_MAKERWORLD_URL = re.compile(r"^https?://(?:www\.)?makerworld\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d+)", re.I)


class SourceError(Exception):
    """A lookup failed in a way worth telling the user about."""


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False)


def parse_url(url: str) -> Optional[tuple]:
    """(provider, id) for a Printables/MakerWorld model URL, else None."""
    url = (url or "").strip()
    for provider, pattern in (("printables", _PRINTABLES_URL), ("makerworld", _MAKERWORLD_URL)):
        match = pattern.match(url)
        if match:
            return provider, match.group(1)
    return None


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


def fetch_details(provider: str, source_id: str) -> dict:
    """Normalised details for one listing; raises SourceError."""
    if provider not in PROVIDERS or not str(source_id).isdigit():
        raise SourceError("Unsupported listing")
    with _client() as client:
        if provider == "printables":
            return _printables_details(client, str(source_id))
        return _makerworld_details(client, str(source_id))


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
    }


# ---------- search ----------

_PRINTABLES_SEARCH = """
query($q: String!, $limit: Int!) { result: searchPrints2(query: $q, printType: print, limit: $limit) {
  items { id name slug user { publicUsername } license { abbreviation name } image { filePath } }
} }
"""


def search(query: str, providers=PROVIDERS, limit: int = 6) -> dict:
    """{'results': [...], 'errors': {provider: message}}. One site being down
    does not hide the other's results."""
    query = (query or "").strip()
    if not query:
        raise SourceError("Type something to search for")
    results, errors = [], {}
    with _client() as client:
        for provider in providers:
            if provider not in PROVIDERS:
                continue
            try:
                found = (_search_printables if provider == "printables" else _search_makerworld)(client, query, limit)
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
        "thumbnail": _printables_media((i.get("image") or {}).get("filePath")),
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
