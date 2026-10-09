"""Two research datasets as model sources: Thingi10K and Objaverse. Each is searched from a small list kept on this server, downloaded once on request.

Thingi10K (Hugging Face, Apache-2.0 code; every model keeps the licence it had on Thingiverse): 10 000 meshes from about 2 000 Thingiverse things made between 2009 and 2015.
The list is three small CSV files (what each thing is called, by whom, under which licence, with which tags and which files); a mesh is one STL file fetched from the
dataset's own repository. The dataset is a frozen snapshot of Thingiverse, so the things are old, and about half of the meshes are not solid.

Objaverse (Hugging Face, allenai/objaverse; the dataset is ODC-By and each object carries its own Creative Commons licence): 800 000+ objects, mostly textured 3D models
from Sketchfab, as GLB. Only the labelled subset (the LVIS categories, about 46 000 objects) is searched, by category name, and each object's name, designer, picture and
licence come from Sketchfab (the public listing of the same object). A GLB is converted to an STL when it is downloaded and scaled to a size that can be printed. Many of these
are meant for screens, not printers, and some licences forbid commercial use or changes (NC, ND): the licence is shown on every result.

Only these two Hugging Face repositories are ever contacted, over https, with redirects checked, with size caps."""
import gzip
import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from app.config import CONFIG_PATH

DIR = CONFIG_PATH / "datasets"
THINGI_REPO = "https://huggingface.co/datasets/Thingi10K/Thingi10K/resolve/main/"
OBJAVERSE_REPO = "https://huggingface.co/datasets/allenai/objaverse/resolve/main/"
HF_DOMAINS = ("huggingface.co", "hf.co")
MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_PATHS_BYTES = 64 * 1024 * 1024
MAX_REDIRECTS = 5
NAMES = ("thingi10k", "objaverse")


class DatasetError(Exception):
    pass


def host_ok(host: Optional[str]) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in HF_DOMAINS)


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=False, headers={"User-Agent": "ModelHub/datasets"})


def fetch(url: str, limit: int) -> bytes:
    """One file from Hugging Face, with every redirect hop checked (https, its own domains) and a size cap."""
    current = url
    with _client() as client:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme != "https" or not host_ok(parsed.hostname):
                raise DatasetError("The dataset pointed to a download on an unexpected address; it was not fetched")
            try:
                with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise DatasetError("The download redirected without saying where")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise DatasetError(f"The dataset did not give the file ({response.status_code})")
                    out, size = [], 0
                    for chunk in response.iter_bytes(1024 * 256):
                        size += len(chunk)
                        if size > limit:
                            raise DatasetError("A dataset file was larger than expected; it was not used")
                        out.append(chunk)
                    return b"".join(out)
            except httpx.HTTPError as e:
                raise DatasetError(f"The dataset could not be reached ({e.__class__.__name__})")
    raise DatasetError("The download redirected too many times")


def _write(name: str, data: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f"{name}.json.tmp"
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(DIR / f"{name}.json")


def _read(name: str) -> Optional[dict]:
    try:
        return json.loads((DIR / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def present(name: str) -> bool:
    return (DIR / f"{name}.json").is_file()


def remove(name: str) -> None:
    (DIR / f"{name}.json").unlink(missing_ok=True)


def status() -> dict:
    out = {}
    for name in NAMES:
        data = _read(name)
        out[name] = ({"present": True, "items": data.get("count", 0), "age_days": round((time.time() - data.get("fetched_at", 0)) / 86400, 1)} if data
                     else {"present": False, "items": 0, "age_days": None})
    return out


# ---------------------------------------------------------------- Thingi10K
_LICENSES = (
    (r"public domain|cc0|dedication", "CC0"),
    (r"attribution.*non-?commercial.*share", "CC BY-NC-SA"), (r"attribution.*non-?commercial.*no deriv", "CC BY-NC-ND"),
    (r"attribution.*non-?commercial", "CC BY-NC"), (r"attribution.*no deriv", "CC BY-ND"), (r"attribution.*share", "CC BY-SA"), (r"attribution", "CC BY"),
    (r"lgpl|lesser", "LGPL"), (r"gpl|general public", "GPL"), (r"bsd", "BSD"),
)


def normalise_license(text: Optional[str]) -> str:
    """'Creative Commons - Attribution - Share Alike' -> 'CC BY-SA' (the text itself when it is not one we know)."""
    raw = str(text or "").strip()
    low = raw.lower()
    if not raw or low in ("none", "unknown", "nan"):
        return ""
    for pattern, label in _LICENSES:
        if re.search(pattern, low):
            return label
    return raw[:80]


def _csv_rows(raw: bytes) -> list:
    import csv
    import io
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))


def build_thingi10k(contextual: list, tags: list, summary: list) -> dict:
    things: dict = {}
    for row in contextual:
        tid = str(row.get("Thing ID") or "").strip()
        if tid.isdigit():
            category = " / ".join(x for x in (row.get("Category"), row.get("Sub-category")) if x and x != "None")
            things[tid] = {"name": (row.get("Name") or "").strip() or f"Thing {tid}", "author": (row.get("Author") or "").strip(), "license": normalise_license(row.get("License")),
                           "category": category, "tags": [], "files": []}
    for row in tags:
        tid, tag = str(row.get("Thing ID") or "").strip(), (row.get("Tag") or "").strip()
        if tid in things and tag and tag.lower() not in things[tid]["tags"] and len(things[tid]["tags"]) < 20:
            things[tid]["tags"].append(tag.lower())
    for row in summary:
        tid, fid = str(row.get("Thing ID") or "").strip(), str(row.get("ID") or "").strip()
        if tid in things and fid.isdigit():
            name = Path(urlparse(row.get("Link") or "").path).name or f"{fid}.stl"
            things[tid]["files"].append({"id": fid, "name": name[:100], "closed": str(row.get("Closed")).upper() == "TRUE"})
            if not things[tid]["license"]:
                things[tid]["license"] = normalise_license(row.get("License"))
    things = {k: v for k, v in things.items() if v["files"]}
    return {"fetched_at": time.time(), "count": len(things), "things": things}


def update_thingi10k() -> dict:
    data = build_thingi10k(_csv_rows(fetch(THINGI_REPO + "metadata/contextual_data.csv", MAX_CSV_BYTES)), _csv_rows(fetch(THINGI_REPO + "metadata/tag_data.csv", MAX_CSV_BYTES)),
                           _csv_rows(fetch(THINGI_REPO + "metadata/input_summary.csv", MAX_CSV_BYTES)))
    if not data["things"]:
        raise DatasetError("The Thingi10K list came back empty")
    _write("thingi10k", data)
    return status()["thingi10k"]


def _words(query: str) -> list:
    return [w for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if w][:8]


def thingi10k_search(query: str, limit: int, page: int = 1) -> list:
    data = _read("thingi10k")
    if not data:
        raise DatasetError("Download the Thingi10K list first (Settings, Dataset sources)")
    words = _words(query)
    if not words:
        raise DatasetError("Type something to search for")
    found = []
    for tid, t in data["things"].items():
        hay = f"{t['name']} {t['author']} {t['category']} {' '.join(t['tags'])}".lower()
        if all(w in hay for w in words):
            found.append((tid, t))
    found.sort(key=lambda kv: (kv[1]["name"].lower(), int(kv[0])))
    start = (max(1, page) - 1) * limit
    return [{"source_id": tid, **t} for tid, t in found[start:start + limit]]


def thingi10k_thing(thing_id: str) -> dict:
    data = _read("thingi10k")
    if not data:
        raise DatasetError("Download the Thingi10K list first (Settings, Dataset sources)")
    thing = data["things"].get(str(thing_id))
    if not thing:
        raise DatasetError("That listing was not found in the Thingi10K list")
    return {"source_id": str(thing_id), **thing}


def thingi10k_file_url(file_id: str) -> str:
    return f"{THINGI_REPO}raw_meshes/{int(file_id)}.stl"


# ---------------------------------------------------------------- Objaverse
def build_objaverse(lvis: dict, paths: dict) -> dict:
    cats, count = {}, 0
    for category, uids in lvis.items():
        rows = []
        for uid in uids:
            path = paths.get(uid)
            match = re.fullmatch(r"glbs/([0-9]{3}-[0-9]{3})/" + re.escape(uid) + r"[.]glb", str(path or ""))
            if re.fullmatch(r"[0-9a-f]{32}", str(uid)) and match:
                rows.append([uid, match.group(1)])
        if rows:
            cats[str(category)] = rows
            count += len(rows)
    return {"fetched_at": time.time(), "count": count, "categories": cats}


def update_objaverse() -> dict:
    lvis = json.loads(gzip.decompress(fetch(OBJAVERSE_REPO + "lvis-annotations.json.gz", MAX_CSV_BYTES)).decode("utf-8"))
    paths = json.loads(gzip.decompress(fetch(OBJAVERSE_REPO + "object-paths.json.gz", MAX_PATHS_BYTES)).decode("utf-8"))
    if not isinstance(lvis, dict) or not isinstance(paths, dict):
        raise DatasetError("The Objaverse lists were not in the expected form")
    data = build_objaverse(lvis, paths)
    del paths
    if not data["categories"]:
        raise DatasetError("The Objaverse list came back empty")
    _write("objaverse", data)
    return status()["objaverse"]


def objaverse_search(query: str, limit: int, page: int = 1) -> list:
    """[{uid, shard, category}] for objects whose LVIS category contains every word (an object's own 32-character id also works)."""
    data = _read("objaverse")
    if not data:
        raise DatasetError("Download the Objaverse list first (Settings, Dataset sources)")
    q = (query or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{32}", q):
        for category, rows in data["categories"].items():
            for uid, shard in rows:
                if uid == q:
                    return [{"uid": uid, "shard": shard, "category": category}]
        return []
    words = _words(query)
    if not words:
        raise DatasetError("Type something to search for")
    flat = []
    for category, rows in sorted(data["categories"].items(), key=lambda kv: (len(kv[0]), kv[0])):
        if all(w in category.replace("_", " ").lower() for w in words):
            flat.extend({"uid": uid, "shard": shard, "category": category} for uid, shard in rows)
    start = (max(1, page) - 1) * limit
    return flat[start:start + limit]


def objaverse_entry(uid: str) -> Optional[dict]:
    data = _read("objaverse")
    if not data:
        raise DatasetError("Download the Objaverse list first (Settings, Dataset sources)")
    for category, rows in data["categories"].items():
        for u, shard in rows:
            if u == uid:
                return {"uid": uid, "shard": shard, "category": category}
    return None


def objaverse_file_url(uid: str, shard: str) -> str:
    return f"{OBJAVERSE_REPO}glbs/{shard}/{uid}.glb"
