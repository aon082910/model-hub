"""Swap filament spools with a Spoolman server (the popular self-hosted spool tracker).

Import turns its spools into Model Hub spools (once each); export creates Model Hub's spools in Spoolman (once each); and, if you
switch it on, every print's grams are also reported to Spoolman, so both keep the same count. Spoolman's address is set in Settings.
Nothing is ever deleted on either side."""
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlmodel import Session, select

from app.models import Filament
from app.settings_store import get_setting

TIMEOUT = httpx.Timeout(10.0, read=30.0)


class SpoolmanError(Exception):
    pass


def base_url(session: Session) -> str:
    raw = (get_setting(session, "spoolman_url", "") or "").strip().rstrip("/")
    if not raw:
        raise SpoolmanError("Enter Spoolman's address first (like http://192.168.1.20:7912)")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SpoolmanError("Spoolman's address must be http(s)://host:port, with no user name or password")
    return raw if raw.endswith("/api/v1") else raw + "/api/v1"


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": "ModelHub/spoolman"})


def _request(client: httpx.Client, method: str, url: str, **kw):
    try:
        response = client.request(method, url, **kw)
    except httpx.TimeoutException:
        raise SpoolmanError("Spoolman did not answer in time")
    except httpx.HTTPError as e:
        raise SpoolmanError(f"Could not reach Spoolman ({e.__class__.__name__})")
    if response.status_code >= 400:
        raise SpoolmanError(f"Spoolman answered with an error ({response.status_code})")
    try:
        return response.json() if response.content else {}
    except ValueError:
        raise SpoolmanError("Spoolman answered with something unexpected")


def test(session: Session) -> str:
    with _client() as client:
        info = _request(client, "GET", base_url(session) + "/info")
    return f"Connected to Spoolman {info.get('version', '')}".strip()


def _hex(value) -> Optional[str]:
    text = str(value or "").lstrip("#")
    return "#" + text.lower() if len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text) else None


def import_spools(session: Session) -> dict:
    """Bring Spoolman's spools in. A spool that was imported before is updated (remaining weight), never added twice."""
    with _client() as client:
        spools = _request(client, "GET", base_url(session) + "/spool", params={"allow_archived": "false"})
    if not isinstance(spools, list):
        raise SpoolmanError("Spoolman answered with something unexpected")
    known = {f.external_id: f for f in session.exec(select(Filament).where(Filament.external_id.is_not(None))).all()}
    added = updated = 0
    for spool in spools[:5000]:
        if not isinstance(spool, dict) or not isinstance(spool.get("id"), int):
            continue
        filament = spool.get("filament") or {}
        vendor = (filament.get("vendor") or {}).get("name")
        weight = filament.get("weight") or spool.get("initial_weight") or 1000
        remaining = spool.get("remaining_weight")
        price = spool.get("price") if spool.get("price") is not None else filament.get("price")
        key = f"spoolman:{spool['id']}"
        row = known.get(key)
        if row:
            if isinstance(remaining, (int, float)) and abs(remaining - row.remaining_g) >= 0.5:
                row.remaining_g = round(float(remaining), 1)
                session.add(row)
                updated += 1
            continue
        session.add(Filament(material=str(filament.get("material") or "PLA")[:40], brand=str(vendor)[:80] if vendor else None, color=str(filament.get("name") or "")[:80] or None,
                             color_hex=_hex(filament.get("color_hex")), spool_weight_g=float(weight), remaining_g=round(float(remaining if isinstance(remaining, (int, float)) else weight), 1),
                             cost=float(price) if isinstance(price, (int, float)) else None, external_id=key, notes="Imported from Spoolman"))
        added += 1
    session.commit()
    return {"added": added, "updated": updated, "seen": len(spools)}


def export_spools(session: Session) -> dict:
    """Create Model Hub's spools that are not in Spoolman yet (a vendor, a filament and a spool each) and remember their ids."""
    base = base_url(session)
    created = 0
    with _client() as client:
        vendors = {str(v.get("name", "")).lower(): v["id"] for v in _request(client, "GET", base + "/vendor") if isinstance(v, dict) and "id" in v}
        for spool in session.exec(select(Filament).where(Filament.external_id.is_(None))).all():
            vendor_id = None
            if spool.brand:
                vendor_id = vendors.get(spool.brand.lower())
                if vendor_id is None:
                    vendor_id = _request(client, "POST", base + "/vendor", json={"name": spool.brand})["id"]
                    vendors[spool.brand.lower()] = vendor_id
            body = {"name": spool.color or spool.material, "material": spool.material, "density": 1.24, "diameter": 1.75, "weight": spool.spool_weight_g}
            if vendor_id:
                body["vendor_id"] = vendor_id
            if _hex(spool.color_hex):
                body["color_hex"] = _hex(spool.color_hex).lstrip("#")
            if spool.cost is not None:
                body["price"] = spool.cost
            filament = _request(client, "POST", base + "/filament", json=body)
            made = _request(client, "POST", base + "/spool", json={"filament_id": filament["id"], "initial_weight": spool.spool_weight_g,
                                                                    "remaining_weight": spool.remaining_g, **({"price": spool.cost} if spool.cost is not None else {})})
            spool.external_id = f"spoolman:{made['id']}"
            session.add(spool)
            session.commit()                      # one at a time, so a failure half way keeps what was done
            created += 1
    return {"created": created}


def report_usage(session: Session, spool: Filament, grams: float) -> None:
    """Tell Spoolman a print used this much of the spool (when switched on and the spool came from or went to Spoolman).
    Never raises: a Spoolman that is down must not stop a print being recorded."""
    import logging
    if get_setting(session, "spoolman_sync_usage", "") != "true" or not (spool.external_id or "").startswith("spoolman:") or grams <= 0:
        return
    try:
        with _client() as client:
            _request(client, "PUT", f"{base_url(session)}/spool/{spool.external_id.split(':', 1)[1]}/use", json={"use_weight": round(float(grams), 2)})
    except Exception as e:
        logging.getLogger("modelhub.spoolman").info("Could not report usage to Spoolman: %s", e.__class__.__name__)
