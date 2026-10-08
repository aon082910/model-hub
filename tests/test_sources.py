"""Matching library models to Printables / MakerWorld listings.

The sites are never contacted: sources._client is swapped for an httpx client
whose transport answers from canned responses shaped like the real APIs.
"""
import io
import json

import httpx
import pytest
from PIL import Image

from app import sources


def _image_bytes(fmt, size=(64, 48), mode="RGB", color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, fmt)
    return out.getvalue()


PNG = _image_bytes("PNG")
JPG = _image_bytes("JPEG")

PRINTABLES_PRINT = {"data": {"print": {
    "id": "3161", "name": "3D BENCHY", "slug": "3d-benchy",
    "summary": "short", "description": "<p>A <b>test</b> boat.</p><ul><li>one</li><li>two</li></ul>",
    "license": {"name": "Creative Commons - Public Domain", "abbreviation": "CC0"},
    "user": {"publicUsername": "Prusa Research"},
    "image": {"filePath": "media/prints/3161/cover.png"},
    "images": [{"filePath": "media/prints/3161/cover.png"}, {"filePath": "media/prints/3161/two.jpg"}],
    "category": {"name": "Test models"}, "tags": [{"name": "Benchy"}, {"name": "Boat"}],
    "likesCount": 10, "downloadCount": 99,
}}}
PRINTABLES_SEARCH = {"data": {"result": {"items": [
    {"id": "3161", "name": "3D BENCHY", "slug": "3d-benchy", "user": {"publicUsername": "Prusa Research"},
     "license": {"abbreviation": "CC0", "name": "x"}, "image": {"filePath": "media/prints/3161/cover.png"}},
    {"id": "9", "name": "Unrelated vase", "slug": "vase", "user": {"publicUsername": "Someone"},
     "license": None, "image": None},
]}}}
MAKERWORLD_DESIGN = {
    "id": 40146, "title": "Benchy Bambu Pla Basic", "slug": "benchy-bambu-pla-basic",
    "summary": "<p>Sliced for A1 mini</p>", "license": "BY-ND", "tags": ["Benchy"],
    "designCreator": {"name": "Bambu Lab"}, "categories": [{"name": "Test Models"}],
    "coverUrl": "https://makerworld.bblmw.com/makerworld/model/X/design/cover.jpg",
    "designExtension": {
        "design_pictures": [{"url": "https://makerworld.bblmw.com/makerworld/model/X/design/a.jpg"}],
        "real_pictures": [{"url": "https://evil.example.com/steal.jpg"}],
    },
    "likeCount": 5, "downloadCount": 6,
}
MAKERWORLD_SEARCH = {"total": 1, "hits": [{
    "id": 40146, "title": "Benchy Bambu Pla Basic", "slug": "benchy-bambu-pla-basic",
    "designCreator": {"name": "Bambu Lab"}, "license": "BY-ND", "cover": "https://makerworld.bblmw.com/c.jpg",
}]}


class FakeSites:
    """Records calls and answers like the two sites; flip the flags to break one."""

    def __init__(self):
        self.calls = []
        self.printables_down = False
        self.makerworld_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        host = request.url.host
        if host == "api.printables.com":
            if self.printables_down:
                return httpx.Response(503)
            body = json.loads(request.content)
            return httpx.Response(200, json=PRINTABLES_SEARCH if "searchPrints2" in body["query"] else PRINTABLES_PRINT)
        if host == "api.bambulab.com":
            if self.makerworld_status != 200:
                return httpx.Response(self.makerworld_status)
            if "/search-service/" in url:
                return httpx.Response(200, json=MAKERWORLD_SEARCH)
            return httpx.Response(200, json=MAKERWORLD_DESIGN)
        if host in sources.IMAGE_HOSTS:
            return httpx.Response(200, content=JPG if url.endswith(".jpg") else PNG)
        raise AssertionError(f"unexpected request to {url}")


@pytest.fixture()
def sites(monkeypatch):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    return fake


# ---------- pure helpers ----------

def test_parse_url():
    assert sources.parse_url("https://www.printables.com/model/3161-3d-benchy") == ("printables", "3161")
    assert sources.parse_url("https://www.printables.com/de/model/3161-x") == ("printables", "3161")
    assert sources.parse_url("https://makerworld.com/en/models/40146-benchy#profileId-1") == ("makerworld", "40146")
    assert sources.parse_url("https://makerworld.com/models/40146") == ("makerworld", "40146")
    for bad in ("https://evil.com/?u=https://www.printables.com/model/1", "http://localhost/model/3",
                "https://www.thingiverse.com/thing:763622", "", "printables.com/model/3"):
        assert sources.parse_url(bad) is None, bad


def test_html_to_text():
    text = sources.html_to_text("<p>A <b>test</b> &amp; more.</p><ul><li>one</li><li>two</li></ul><br>end")
    assert text == "A test & more.\n- one\n- two\n\nend"
    assert sources.html_to_text(None) == ""


def test_suggest_query_and_ranking():
    assert sources.suggest_query("Benchy_v2-fixed (1).stl") == "Benchy"
    assert sources.suggest_query("PhoneStand_Final.3mf") == "Phone Stand"
    assert sources.suggest_query("a1b2c3d4e5f6.stl") == ""            # hash-like names carry no information
    ranked = sources.rank([{"title": "Unrelated vase"}, {"title": "3D Benchy"}], "benchy")
    assert ranked[0]["title"] == "3D Benchy" and ranked[0]["score"] > ranked[1]["score"]


# ---------- site access ----------

def test_fetch_details_printables(sites):
    d = sources.fetch_details("printables", "3161")
    assert d["title"] == "3D BENCHY" and d["designer"] == "Prusa Research" and d["license"] == "CC0"
    assert d["tags"] == ["Benchy", "Boat"] and d["category"] == "Test models"
    assert d["description"] == "A test boat.\n- one\n- two"
    assert d["images"] == ["https://media.printables.com/media/prints/3161/cover.png",
                           "https://media.printables.com/media/prints/3161/two.jpg"]   # de-duplicated
    assert d["url"] == "https://www.printables.com/model/3161-3d-benchy"


def test_fetch_details_makerworld(sites):
    d = sources.fetch_details("makerworld", "40146")
    assert d["designer"] == "Bambu Lab" and d["license"] == "BY-ND" and d["description"] == "Sliced for A1 mini"
    assert d["images"][0].endswith("design/cover.jpg")                 # cover first
    assert d["url"] == "https://makerworld.com/en/models/40146-benchy-bambu-pla-basic"


@pytest.mark.parametrize("status,fragment", [(404, "not found"), (429, "rate limiting"), (500, r"error \(500\)")])
def test_makerworld_errors_become_messages(sites, status, fragment):
    sites.makerworld_status = status
    with pytest.raises(sources.SourceError, match=fragment):
        sources.fetch_details("makerworld", "1")


def test_fetch_details_rejects_bad_ids(sites):
    for provider, sid in (("thingiverse", "1"), ("printables", "abc"), ("makerworld", "../1")):
        with pytest.raises(sources.SourceError):
            sources.fetch_details(provider, sid)
    assert sites.calls == []                                           # nothing was even requested


def test_search_one_site_down_keeps_the_other(sites):
    sites.printables_down = True
    found = sources.search("benchy")
    assert [r["provider"] for r in found["results"]] == ["makerworld"]
    assert "printables" in found["errors"]


def test_download_image_checks_host_and_content(sites):
    client = sources._client()
    with pytest.raises(sources.SourceError, match="supported site"):
        sources.download_image(client, "https://evil.example.com/a.jpg")
    with pytest.raises(sources.SourceError, match="supported site"):
        sources.download_image(client, "http://media.printables.com/a.jpg")        # not https
    assert sources.download_image(client, "https://media.printables.com/a.png") == (PNG, ".png")
    # the extension comes from the bytes: a .jpg URL that actually returns text is refused
    sites_text = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>")))
    with pytest.raises(sources.SourceError, match="not a supported image"):
        sources.download_image(sites_text, "https://media.printables.com/a.jpg")
    big = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"\xff\xd8\xff" + b"0" * (sources.MAX_IMAGE_BYTES + 1))))
    with pytest.raises(sources.SourceError, match="too large"):
        sources.download_image(big, "https://media.printables.com/a.jpg")


def test_shrink_image_bounds_size_and_flattens_transparency():
    big = _image_bytes("PNG", size=(3000, 1500), mode="RGBA", color=(0, 0, 0, 0))
    out = Image.open(io.BytesIO(sources.shrink_image(big)))
    assert out.format == "JPEG" and out.size == (1200, 600)
    assert out.getpixel((10, 10))[0] > 200                     # transparent became light grey, not black
    small = Image.open(io.BytesIO(sources.shrink_image(_image_bytes("JPEG", size=(80, 40)))))
    assert small.size == (80, 40)                              # never scaled up


def test_shrink_image_refuses_huge_or_broken_pictures(monkeypatch):
    monkeypatch.setattr(sources, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(sources.SourceError, match="too large"):
        sources.shrink_image(_image_bytes("PNG", size=(100, 100)))
    monkeypatch.undo()
    with pytest.raises(sources.SourceError, match="could not be read"):
        sources.shrink_image(b"\x89PNG\r\n\x1a\n" + b"not really a png")


# ---------- API ----------

def _import_model(c, filename):
    stl = b"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 9 0 0\n  vertex 0 9 0\n endloop\nendfacet\nendsolid t\n"
    r = c.post("/api/library/import", files={"file": (filename, stl, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def test_search_and_lookup_endpoints(authed, sites):
    r = authed.get("/api/sources/search?q=benchy").json()
    assert {x["provider"] for x in r["results"]} == {"printables", "makerworld"}
    assert r["results"][0]["score"] >= r["results"][-1]["score"]
    assert authed.get("/api/sources/search?q=benchy&provider=makerworld").json()["results"][0]["provider"] == "makerworld"
    assert authed.get("/api/sources/search?q=%20").status_code == 400

    d = authed.get("/api/sources/lookup", params={"url": "https://www.printables.com/model/3161-3d-benchy"}).json()
    assert d["title"] == "3D BENCHY"
    assert authed.get("/api/sources/lookup", params={"url": "https://evil.com/model/1"}).status_code == 400
    sites.makerworld_status = 404
    assert authed.get("/api/sources/lookup", params={"url": "https://makerworld.com/en/models/5"}).status_code == 502


def test_suggest_for_a_model(authed, sites):
    model = _import_model(authed, "Benchy_v2.stl")
    r = authed.get(f"/api/library/models/{model['id']}/source/suggest").json()
    assert r["query"] == "Benchy"
    assert r["results"][0]["title"].lower().count("benchy") == 1 or "benchy" in r["results"][0]["title"].lower()
    assert authed.get(f"/api/library/models/{model['id']}/source/suggest", params={"q": "vase"}).json()["query"] == "vase"
    assert authed.get("/api/library/models/999999/source/suggest").status_code == 404


def test_link_fills_details_stores_images_and_unlinks(authed, sites):
    model = _import_model(authed, "link-me.stl")
    mid = model["id"]
    r = authed.post(f"/api/library/models/{mid}/source", json={"url": "https://www.printables.com/model/3161-3d-benchy"})
    assert r.status_code == 200, r.text
    m = r.json()
    assert (m["source_provider"], m["source_id"], m["source_title"]) == ("printables", "3161", "3D BENCHY")
    assert m["designer"] == "Prusa Research" and m["license"] == "CC0"
    assert m["source_description"].startswith("A test boat.")
    assert m["source_url"] == "https://www.printables.com/model/3161-3d-benchy"
    names = json.loads(m["source_images"])
    assert names == ["1.jpg", "2.jpg"]                              # always re-saved as JPEG

    img = authed.get(f"/api/library/models/{mid}/source/images/1.jpg")
    assert img.status_code == 200 and img.content[:3] == b"\xff\xd8\xff"
    # only plain stored names are served -- no path tricks, nothing for other ids
    for bad in ("..%2F..%2Fmodelhub.db", "9.jpg", "1.exe", "1.jpg%00"):
        assert authed.get(f"/api/library/models/{mid}/source/images/{bad}").status_code in (404, 422), bad
    assert authed.get("/api/library/models/999999/source/images/1.jpg").status_code == 404

    # site tags were not added unless asked
    assert authed.get(f"/api/library/models/{mid}").json().get("source_tags") == json.dumps(["Benchy", "Boat"])

    un = authed.delete(f"/api/library/models/{mid}/source").json()
    assert un["source_provider"] is None and un["source_images"] is None and un["source_url"] is None
    assert authed.get(f"/api/library/models/{mid}/source/images/1.jpg").status_code == 404


def test_link_by_provider_id_and_options(authed, sites):
    model = _import_model(authed, "link-opts.stl")
    mid = model["id"]
    authed.patch(f"/api/library/models/{mid}", json={"designer": "Me", "license": "Mine"})
    r = authed.post(f"/api/library/models/{mid}/source", json={
        "provider": "makerworld", "source_id": "40146", "images": False, "fill_details": False, "add_tags": True,
    })
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["designer"] == "Me" and m["license"] == "Mine"            # fill_details off: left alone
    assert m["source_images"] is None                                   # images off
    assert m["source_provider"] == "makerworld"
    tags = authed.get("/api/tags").json()
    assert "benchy" in {t["name"] for t in tags}
    # the real-life picture on a non-allowlisted host was never fetched
    assert not any("evil.example.com" in c for c in sites.calls)


def test_failed_lookup_leaves_model_untouched(authed, sites):
    model = _import_model(authed, "link-fail.stl")
    mid = model["id"]
    sites.makerworld_status = 429
    r = authed.post(f"/api/library/models/{mid}/source", json={"provider": "makerworld", "source_id": "1"})
    assert r.status_code == 502 and "rate limiting" in r.json()["detail"]
    assert authed.get(f"/api/library/models/{mid}").json()["source_provider"] is None
    assert authed.post(f"/api/library/models/{mid}/source", json={"url": "https://evil.com/model/1"}).status_code == 400
    assert authed.post(f"/api/library/models/{mid}/source", json={}).status_code == 400


def test_extension_import_with_source_url_fills_details(authed, sites):
    stl = b"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 7 0 0\n  vertex 0 7 0\n endloop\nendfacet\nendsolid t\n"
    r = authed.post("/api/library/import",
                    files={"file": ("ext-import.stl", stl, "application/octet-stream")},
                    data={"source_url": "https://makerworld.com/en/models/40146-benchy-bambu-pla-basic"})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["source_provider"] == "makerworld" and m["designer"] == "Bambu Lab"
    assert json.loads(m["source_images"])


def test_extension_import_survives_a_site_outage(authed, sites):
    sites.makerworld_status = 500
    stl = b"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 6 0 0\n  vertex 0 6 0\n endloop\nendfacet\nendsolid t\n"
    r = authed.post("/api/library/import",
                    files={"file": ("ext-outage.stl", stl, "application/octet-stream")},
                    data={"source_url": "https://makerworld.com/en/models/40146-x"})
    assert r.status_code == 200                                         # the file still got imported
    assert r.json()["source_provider"] is None
    assert r.json()["source_url"] == "https://makerworld.com/en/models/40146-x"


def test_deleting_a_model_removes_its_pictures(authed, sites):
    model = _import_model(authed, "link-delete.stl")
    mid = model["id"]
    authed.post(f"/api/library/models/{mid}/source", json={"provider": "printables", "source_id": "3161"})
    assert sources.image_path(mid, "1.jpg") is not None
    assert authed.delete(f"/api/library/models/{mid}").status_code == 200
    assert sources.image_path(mid, "1.jpg") is None


def test_models_list_and_detail_include_tags(authed, sites):
    model = _import_model(authed, "tags-visible.stl")
    mid = model["id"]
    linked = authed.post(f"/api/library/models/{mid}/source", json={
        "provider": "printables", "source_id": "3161", "images": False, "add_tags": True}).json()
    assert [t["name"] for t in linked["tags"]] == ["benchy", "boat"]

    listed = next(m for m in authed.get("/api/library/models?limit=5000").json() if m["id"] == mid)
    assert [t["name"] for t in listed["tags"]] == ["benchy", "boat"]
    assert "embedding" not in listed
    assert [t["name"] for t in authed.get(f"/api/library/models/{mid}").json()["tags"]] == ["benchy", "boat"]
    untagged = _import_model(authed, "tags-none.stl")
    assert authed.get(f"/api/library/models/{untagged['id']}").json()["tags"] == []


# ---------- parts lists (MakerWorld) ----------

PARTS_EXTENSION = {
    "boms": [
        {"sku": "B-AA041", "quantity": 2, "url": "https://store.bambulab.com/products/m3?id=1",
         "title": "M3x25 SHCS Machine Screw (5PCS) - AA041", "displayTitle": "M3x25 SHCS Machine Screw (5PCS) - AA041",
         "parentTitle": "M3 Socket Head Cap Machine Screws (SHCS)",
         "displayParentTitle": "M3 Socket Head Cap Machine Screws (SHCS)", "priceInfo": {"priceX100": 0}},
        {"sku": "B-PG001", "quantity": 4, "url": "http://insecure.example.com/x", "title": "9g Servo (1PCS)",
         "parentTitle": "9g Servo Motor", "priceInfo": {"priceX100": 0}},
        {"sku": "B-NOPRICE", "quantity": 1, "title": "Lamp Base", "priceInfo": {"priceX100": 1250, "code": "USD"}},
    ],
    "boms_v2": [{"productSkuList": [{"sku": "b-aa041", "price": 1.06, "currency": "USD"},
                                    {"sku": "b-pg001", "price": 5.49, "currency": "EUR"}]}],
    "boms_of_other_part_list": [
        {"name": "Arduino Uno", "quantity": 1, "note": ""},
        {"name": "- HC-SR04 Distance Sensor\n- 2mm diameter screws\n- 3 x wires", "quantity": 1},
    ],
    "boms_of_other_parts": "- HC-SR04 Distance Sensor\n- 2x MG90S servo\n1. Super glue",
    "boms_of_filaments": [{"title": "PLA Basic"}],
}


def test_classify_part():
    cases = {
        "M3 Button Head Cap Machine Screws": "parts", "608ZZ bearing": "parts", "Heat-set insert M3": "parts",
        "MG90S Servo": "electronics", "Arduino Uno or Mega": "electronics", "Jumper Cables": "electronics",
        "Puck Lights": "electronics", "Super glue": "supplies", "Solder wire": "supplies",
        "Cable ties": "supplies", "Mystery widget": "parts",
    }
    for name, expected in cases.items():
        assert sources.classify_part(name) == expected, name


def test_parse_free_parts():
    rows = sources.parse_free_parts("- 2x MG90S servo\n* Jumper wires x10\n  3 M3 screws\n2mm diameter screws\n1. DeskPi board\n\n")
    assert [(r["name"], r["quantity"]) for r in rows] == [
        ("MG90S servo", 2), ("Jumper wires", 10), ("M3 screws", 3), ("2mm diameter screws", 1), ("DeskPi board", 1)]
    assert sources.parse_free_parts(None) == []


def test_makerworld_parts():
    rows = sources.makerworld_parts(PARTS_EXTENSION)
    by_name = {r["name"]: r for r in rows}
    screw = by_name["M3 Socket Head Cap Machine Screws (SHCS) - M3x25 SHCS Machine Screw (5PCS) - AA041"]
    assert (screw["quantity"], screw["unit_cost"], screw["category"], screw["kind"]) == (2, 1.06, "parts", "store")
    assert screw["purchase_url"].startswith("https://store.bambulab.com/")
    assert "sold in packs" in screw["notes"]                    # "x2 of a 5-pack": the person decides how many to buy

    servo = by_name["9g Servo Motor - 9g Servo (1PCS)"]
    assert servo["unit_cost"] == 5.49 and servo["quantity"] == 4 and servo["category"] == "electronics"
    assert servo["purchase_url"] is None                          # only https links are kept
    assert "EUR" in servo["notes"]

    assert by_name["Lamp Base"]["unit_cost"] == 12.5              # falls back to the item's own price

    assert by_name["Arduino Uno"]["kind"] == "listed"
    assert by_name["wires"]["quantity"] == 3
    assert by_name["MG90S servo"]["quantity"] == 2
    assert by_name["Super glue"]["category"] == "supplies"
    names = [r["name"].lower() for r in rows]
    assert names.count("hc-sr04 distance sensor") == 1           # listed twice, shown once
    assert not any("pla basic" in n for n in names)               # filament is tracked separately
    assert sources.makerworld_parts({}) == []


def test_parts_endpoint(authed, sites, monkeypatch):
    monkeypatch.setitem(MAKERWORLD_DESIGN, "designExtension", {**MAKERWORLD_DESIGN["designExtension"], **PARTS_EXTENSION})
    r = authed.get("/api/sources/parts", params={"url": "https://makerworld.com/en/models/40146-benchy"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["listing"]["title"] == "Benchy Bambu Pla Basic" and data["note"] == ""
    assert len(data["parts"]) >= 8
    assert all(p["category"] in ("electronics", "parts", "supplies") for p in data["parts"])

    pr = authed.get("/api/sources/parts", params={"provider": "printables", "source_id": "3161"}).json()
    assert pr["parts"] == [] and "Printables" in pr["note"]
    assert authed.get("/api/sources/parts").status_code == 400
    assert authed.get("/api/sources/parts", params={"url": "https://evil.com/models/1"}).status_code == 400
    sites.makerworld_status = 404
    assert authed.get("/api/sources/parts", params={"url": "https://makerworld.com/en/models/1"}).status_code == 502


def test_printables_search_thumbnails_are_small_versions(sites):
    thumb = sources._printables_thumbnail
    assert thumb("media/prints/3161/images/20206_x/benchy.jpg") == \
        "https://media.printables.com/media/prints/3161/images/20206_x/thumbs/inside/320x320/jpg/benchy.jpg"
    assert thumb("media/prints/1/images/a/Cover.PNG").endswith("/thumbs/inside/320x320/png/Cover.PNG")
    assert thumb("media/prints/1/images/a/pic.jpeg") == "https://media.printables.com/media/prints/1/images/a/pic.jpeg"
    assert thumb(None) is None
    found = sources.search("benchy", providers=("printables",))
    assert "/thumbs/inside/320x320/" in found["results"][0]["thumbnail"]
