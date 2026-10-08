"""The Search page's API: the library and every site at once, and the listing preview."""
import httpx
import pytest

from app import downloads, sources
from test_sources import FakeSites, SKETCHFAB_UID


@pytest.fixture()
def sites(monkeypatch, authed):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(downloads, "_ensure_worker", lambda: None)
    downloads._items.clear()
    yield fake
    downloads._items.clear()


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


def _model(c, name, n):
    r = c.post("/api/library/import", files={"file": (name, _stl(n), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def test_search_requires_a_query_and_a_login(authed, sites):
    assert authed.get("/api/discover/search", params={"q": "  "}).status_code == 400
    assert authed.get("/api/discover/search").status_code == 422
    authed.post("/api/auth/logout")
    assert authed.get("/api/discover/search", params={"q": "benchy"}).status_code == 401
    from conftest import ensure_authenticated
    ensure_authenticated(authed)


def test_search_covers_the_library_and_every_usable_site(authed, sites):
    named = _model(authed, "discover_benchy_named.stl", 61)
    tagged = _model(authed, "discover_plain_one.stl", 62)
    authed.post(f"/api/tags/models/{tagged['id']}", json={"name": "benchy"})
    noted = _model(authed, "discover_plain_two.stl", 63)
    authed.patch(f"/api/library/models/{noted['id']}", json={"notes": "my benchy variant"})
    _model(authed, "discover_unrelated.stl", 64)

    data = authed.get("/api/discover/search", params={"q": "benchy"}).json()
    assert data["query"] == "benchy" and data["page"] == 1
    library_ids = {m["id"] for m in data["library"]}
    assert {named["id"], tagged["id"], noted["id"]} <= library_ids               # found by name, tag and notes
    assert all("tags" in m for m in data["library"])
    assert {r["provider"] for r in data["online"]} == {"printables", "makerworld", "sketchfab", "commons"}
    assert set(data["searched"]) == {"printables", "makerworld", "sketchfab", "commons", "nasa3d", "smithsonian", "archive"}
    assert data["errors"] == {}
    by_provider = {r["provider"]: r for r in data["online"]}
    assert by_provider["printables"]["can_download"] is True and by_provider["makerworld"]["can_download"] is False
    scores = [r["score"] for r in data["online"]]
    assert scores == sorted(scores, reverse=True)
    assert all(r["in_library"] is None for r in data["online"])


def test_library_results_can_be_left_out_and_only_come_on_page_one(authed, sites):
    _model(authed, "discover_benchy_flag.stl", 65)
    assert authed.get("/api/discover/search", params={"q": "benchy", "library": "false"}).json()["library"] == []
    assert authed.get("/api/discover/search", params={"q": "benchy", "page": 2}).json()["library"] == []


def test_choosing_sites_and_paging(authed, sites):
    data = authed.get("/api/discover/search", params={"q": "benchy", "providers": "sketchfab,bogus", "page": 3, "limit": 5}).json()
    assert data["searched"] == ["sketchfab"]
    assert {r["provider"] for r in data["online"]} == {"sketchfab"} and data["page"] == 3 and data["limit"] == 5
    # a site that needs a key says so instead of silently returning nothing
    keyed = authed.get("/api/discover/search", params={"q": "benchy", "providers": "thingiverse"}).json()
    assert keyed["online"] == [] and "access token" in keyed["errors"]["thingiverse"]
    assert authed.get("/api/discover/search", params={"q": "x", "limit": 99}).status_code == 422
    assert authed.get("/api/discover/search", params={"q": "x", "page": 0}).status_code == 422


def test_has_more_when_a_site_filled_its_page(authed, sites):
    full = authed.get("/api/discover/search", params={"q": "benchy", "providers": "makerworld", "limit": 1}).json()
    assert len(full["online"]) == 1 and full["has_more"] is True
    short = authed.get("/api/discover/search", params={"q": "benchy", "providers": "makerworld", "limit": 10}).json()
    assert short["has_more"] is False


def test_one_site_down_is_reported_not_fatal(authed, sites):
    sites.printables_down = True
    data = authed.get("/api/discover/search", params={"q": "benchy"}).json()
    assert "printables" in data["errors"] and {r["provider"] for r in data["online"]} == {"makerworld", "sketchfab", "commons"}


def test_results_already_linked_to_a_library_model_are_marked(authed, sites):
    model = _model(authed, "discover_linked.stl", 66)
    authed.post(f"/api/library/models/{model['id']}/source", json={"provider": "sketchfab", "source_id": SKETCHFAB_UID, "images": False})
    data = authed.get("/api/discover/search", params={"q": "benchy"}).json()
    hit = next(r for r in data["online"] if r["provider"] == "sketchfab" and r["source_id"] == SKETCHFAB_UID)
    assert hit["in_library"] == model["id"]
    # and it is a library result too, because its listing title is searchable
    assert model["id"] in {m["id"] for m in data["library"]}
    authed.delete(f"/api/library/models/{model['id']}/source")


# ---------- the listing preview ----------

def test_listing_for_a_downloadable_site(authed, sites):
    data = authed.get("/api/discover/listing", params={"provider": "printables", "source_id": "3161"}).json()
    assert data["details"]["title"] == "3D BENCHY" and data["details"]["designer"] == "Prusa Research"
    assert data["can_download"] is True and data["download_note"] is None
    # every model file is listed; the pack of model files is the default choice, the single STL is optional
    files = {f["name"]: f for f in data["files"]}
    assert set(files) == {"model-files-3161.zip", "3dbenchy.stl"}
    assert files["model-files-3161.zip"]["kind"] == "pack" and files["model-files-3161.zip"]["selected"] is True
    assert files["3dbenchy.stl"]["selected"] is False and files["3dbenchy.stl"]["selectable"] is True
    assert data["in_library"] == [] and data["download"] is None and data["files_note"] is None


def test_listing_by_url_and_for_a_site_that_cannot_be_downloaded_from(authed, sites):
    data = authed.get("/api/discover/listing", params={"url": "https://makerworld.com/en/models/40146-benchy"}).json()
    assert data["details"]["title"] == "Benchy Bambu Pla Basic"
    assert data["can_download"] is False and "browser extension" in data["download_note"]
    assert data["files"] is None
    assert authed.get("/api/discover/listing", params={"url": "https://evil.com/model/1"}).status_code == 400
    assert authed.get("/api/discover/listing").status_code == 400


def test_listing_failures_and_file_problems(authed, sites):
    sites.makerworld_status = 404
    assert authed.get("/api/discover/listing", params={"provider": "makerworld", "source_id": "1"}).status_code == 502
    sites.printables_files = {"data": {"print": {"id": "3161", "stls": [], "downloadPacks": []}}}
    data = authed.get("/api/discover/listing", params={"provider": "printables", "source_id": "3161"}).json()
    assert data["files"] == [] and "no model files" in data["files_note"]


def test_listing_shows_what_is_already_in_the_library_and_the_queue(authed, sites):
    from test_downloads import _forget_downloaded_models
    _forget_downloaded_models()
    downloads.enqueue([{"provider": "printables", "source_id": "3161", "title": "3D BENCHY"}])
    queued = authed.get("/api/discover/listing", params={"provider": "printables", "source_id": "3161"}).json()
    assert queued["download"]["status"] == "queued" and queued["in_library"] == []
    item = downloads._items[0]
    item["status"] = "downloading"
    downloads.process_item(item)
    done = authed.get("/api/discover/listing", params={"provider": "printables", "source_id": "3161"}).json()
    assert done["download"]["status"] == "done" and len(done["in_library"]) == 2
    _forget_downloaded_models()
