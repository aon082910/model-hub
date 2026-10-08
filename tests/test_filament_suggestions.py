"""Filament a MakerWorld listing suggests, matched against your own spools."""
import json

import httpx
import pytest

from app import sources
from app.filament_match import match_spool
from app.models import Filament
from test_sources import MAKERWORLD_DESIGN, FakeSites


@pytest.fixture()
def sites(monkeypatch):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    return fake


def _spool(material, color, remaining=500, brand="Test"):
    return Filament(id=None, material=material, color=color, brand=brand, remaining_g=remaining)


def test_match_spool_rules():
    gray_pla = _spool("PLA", "Grey", 300)
    other_gray = _spool("PLA", "Gray", 800)
    dark = _spool("PLA", "Dark Gray", 999)
    petg = _spool("PETG", "Gray", 999)
    suggestion = {"material": "PLA", "color": "Gray"}

    # spelling variants count as the same colour; the fuller spool wins a tie; other materials never match
    assert match_spool(suggestion, [gray_pla, other_gray, petg]) is other_gray
    assert match_spool(suggestion, [petg]) is None
    # a close colour name is the fallback when nothing is exact
    assert match_spool(suggestion, [dark, petg]) is dark
    assert match_spool({"material": "PLA", "color": "Jade White"}, [gray_pla]) is None
    assert match_spool({"material": "PLA", "color": ""}, [gray_pla]) is None
    assert match_spool({"material": "", "color": "Gray"}, [gray_pla]) is None
    # the material is compared by its first word, so a spool labelled 'PLA Basic' still counts as PLA
    assert match_spool(suggestion, [_spool("pla basic", "gray")]) is not None


def _import(c, name, n):
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
           " endloop\nendfacet\nendsolid t\n").encode()
    r = c.post("/api/library/import", files={"file": (name, stl, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def test_suggestions_for_a_linked_model(authed, sites, monkeypatch):
    monkeypatch.setitem(MAKERWORLD_DESIGN, "designExtension", {**MAKERWORLD_DESIGN["designExtension"], "boms_of_filaments": [
        {"parentTitle": "PLA Basic", "title": "Gray (10103) / Refill / 1kg"},
        {"parentTitle": "PETG HF", "title": "Black (33102) / Refill / 1kg"},
        {"parentTitle": "ABS", "title": "Red (40000) / Refill / 1kg"},
    ]})
    model = _import(authed, "fil-suggest.stl", 4)
    linked = authed.post(f"/api/library/models/{model['id']}/source",
                         json={"provider": "makerworld", "source_id": "40146", "images": False}).json()
    assert [f["material"] for f in json.loads(linked["source_filaments"])] == ["PLA", "PETG", "ABS"]

    pid = authed.post("/api/projects", json={"name": "Suggestion project"}).json()["id"]
    authed.post(f"/api/projects/{pid}/models/{model['id']}")
    gray = authed.post("/api/filament", json={"material": "PLA", "brand": "Hatchbox", "color": "Grey", "remaining_g": 700}).json()
    black = authed.post("/api/filament", json={"material": "PETG", "brand": "Sunlu", "color": "Black", "remaining_g": 50}).json()

    url = f"/api/projects/{pid}/models/{model['id']}/filament-suggestions"
    by_material = {r["material"]: r for r in authed.get(url).json()}
    assert by_material["PLA"]["spool_id"] == gray["id"] and by_material["PLA"]["spool_label"] == "PLA Hatchbox Grey"
    assert by_material["PETG"]["spool_id"] == black["id"] and by_material["PETG"]["remaining_g"] == 50
    assert by_material["ABS"]["spool_id"] is None and by_material["ABS"]["spool_label"] is None
    assert by_material["PLA"]["label"] == "PLA Basic Gray" and not by_material["PLA"]["already_added"]

    # once a line using that spool exists it is reported as added
    authed.post(f"/api/projects/{pid}/models/{model['id']}/filament", json={"filament_id": gray["id"], "grams": 12})
    again = {r["material"]: r for r in authed.get(url).json()}
    assert again["PLA"]["already_added"] is True and again["PETG"]["already_added"] is False

    assert authed.get(f"/api/projects/{pid}/models/999999/filament-suggestions").status_code == 404
    assert authed.get(f"/api/projects/999999/models/{model['id']}/filament-suggestions").status_code == 404


def test_model_without_a_listing_has_no_suggestions(authed):
    model = _import(authed, "fil-none.stl", 3)
    pid = authed.post("/api/projects", json={"name": "No suggestion project"}).json()["id"]
    authed.post(f"/api/projects/{pid}/models/{model['id']}")
    assert authed.get(f"/api/projects/{pid}/models/{model['id']}/filament-suggestions").json() == []
