"""Spool presets from the Open Filament Database (community-maintained, MIT licence): brand, filament, colour and the weights sold.

One download of four small CSV files from api.openfilamentdatabase.org (about 6 MB) is turned into a compact local list you can search; nothing else
is ever fetched from there, and nothing is sent to it. Update it whenever you like."""
import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import CONFIG_PATH

BASE = "https://api.openfilamentdatabase.org/csv/"
FILES = ("brands", "filaments", "variants", "sizes")
CACHE = CONFIG_PATH / "filament_db.json"
MAX_BYTES = 12 * 1024 * 1024
MAX_AGE_DAYS = 60


class DatabaseError(Exception):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=60, follow_redirects=False, headers={"User-Agent": "ModelHub/filament-db"})


def _fetch(name: str) -> list:
    try:
        with _client() as client:
            response = client.get(f"{BASE}{name}.csv")
    except Exception as e:
        raise DatabaseError(f"The database could not be reached ({e.__class__.__name__})")
    if response.status_code != 200 or len(response.content) > MAX_BYTES:
        raise DatabaseError(f"The database did not give {name} ({response.status_code})")
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig", errors="replace"))))


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def build(brands: list, filaments: list, variants: list, sizes: list) -> list:
    """One row per colour: {brand, name, material, color, hex, grams: [sizes sold], density, nozzle: [min, max], bed: [min, max]}."""
    brand_name = {b["id"]: b.get("name") or "" for b in brands}
    filament = {f["id"]: f for f in filaments}
    weights: dict = {}
    for size in sizes:
        grams = _num(size.get("filament_weight"))
        if grams and size.get("discontinued") != "1":
            weights.setdefault(size["variant_id"], set()).add(int(grams))
    out = []
    for v in variants:
        f = filament.get(v.get("filament_id"))
        if not f or v.get("discontinued") == "1" or f.get("discontinued") == "1":
            continue
        color_hex = (v.get("color_hex") or "").strip()
        out.append({"brand": brand_name.get(f.get("brand_id"), ""), "name": f.get("name") or "", "material": (f.get("material") or "").strip(),
                    "color": (v.get("name") or "").strip(), "hex": color_hex if re.fullmatch(r"#[0-9A-Fa-f]{6}", color_hex) else None,
                    "grams": sorted(weights.get(v["id"], [])), "density": _num(f.get("density")),
                    "nozzle": [_num(f.get("min_print_temperature")), _num(f.get("max_print_temperature"))],
                    "bed": [_num(f.get("min_bed_temperature")), _num(f.get("max_bed_temperature"))]})
    return out


def update() -> dict:
    data = {name: _fetch(name) for name in FILES}
    rows = build(data["brands"], data["filaments"], data["variants"], data["sizes"])
    if not rows:
        raise DatabaseError("The database came back empty")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "rows": rows}, separators=(",", ":")), encoding="utf-8")
    tmp.replace(CACHE)
    return status()


def _load() -> Optional[dict]:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status() -> dict:
    data = _load()
    if not data:
        return {"present": False, "colours": 0, "age_days": None, "old": False}
    age = (time.time() - data.get("fetched_at", 0)) / 86400
    return {"present": True, "colours": len(data["rows"]), "age_days": round(age, 1), "old": age > MAX_AGE_DAYS}


def search(query: str, limit: int = 30) -> list:
    data = _load()
    if not data:
        raise DatabaseError("Download the database first")
    words = [w for w in re.split(r"\s+", (query or "").lower().strip()) if w]
    if not words:
        return []
    out = []
    for row in data["rows"]:
        hay = f"{row['brand']} {row['name']} {row['material']} {row['color']}".lower()
        if all(w in hay for w in words):
            out.append(row)
            if len(out) >= limit:
                break
    return out
