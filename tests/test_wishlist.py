"""The wishlist: save listings for later, mark them in search results, download them all."""
import httpx
import pytest

from app import downloads, sources
from test_sources import FakeSites


@pytest.fixture(autouse=True)
def sites(monkeypatch, authed):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(downloads, "_ensure_worker", lambda: None)
    downloads._items.clear()
    _empty(authed)
    yield fake
    _empty(authed)
    downloads._items.clear()


def _empty(c):
    for item in c.get("/api/wishlist").json():
        c.delete(f"/api/wishlist/{item['id']}")


def _add(c, **fields):
    r = c.post("/api/wishlist", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


def test_save_with_details_and_list_newest_first(authed):
    first = _add(authed, provider="printables", source_id="3161", title="3D BENCHY", designer="Prusa", note="for the kids")
    second = _add(authed, provider="makerworld", source_id="40146", title="Benchy Bambu")
    assert first["note"] == "for the kids" and first["can_download"] is True and first["label"] == "Printables"
    assert second["can_download"] is False and "browser extension" in second["download_note"]
    assert [i["id"] for i in authed.get("/api/wishlist").json()] == [second["id"], first["id"]]


def test_saving_twice_returns_the_same_item(authed):
    a = _add(authed, provider="printables", source_id="3161", title="3D BENCHY")
    b = _add(authed, provider="printables", source_id="3161", title="Another title")
    assert a["id"] == b["id"] and len(authed.get("/api/wishlist").json()) == 1


def test_saving_by_id_only_looks_the_listing_up(authed, sites):
    item = _add(authed, provider="printables", source_id="3161")
    assert item["title"] == "3D BENCHY" and item["designer"] == "Prusa Research" and item["license"] == "CC0"
    assert item["thumbnail"] and item["url"].startswith("https://www.printables.com/model/3161")


def test_a_failed_lookup_saves_nothing(authed, sites):
    sites.makerworld_status = 404
    assert authed.post("/api/wishlist", json={"provider": "makerworld", "source_id": "1"}).status_code == 502
    assert authed.get("/api/wishlist").json() == []


@pytest.mark.parametrize("payload", [{}, {"provider": "nowhere", "source_id": "1"}, {"provider": "printables", "source_id": "abc"},
                                     {"provider": "sketchfab", "source_id": "short"}])
def test_bad_listings_are_refused(authed, payload):
    assert authed.post("/api/wishlist", json=payload).status_code == 400


def test_notes_can_be_edited_and_items_removed(authed):
    item = _add(authed, provider="printables", source_id="3161", title="3D BENCHY")
    r = authed.patch(f"/api/wishlist/{item['id']}", json={"note": "  later  "}).json()
    assert r["note"] == "later"
    assert authed.patch(f"/api/wishlist/{item['id']}", json={"note": ""}).json()["note"] is None
    assert authed.patch(f"/api/wishlist/{item['id']}", json={"note": 5}).status_code == 400
    assert authed.delete(f"/api/wishlist/{item['id']}").status_code == 200
    assert authed.delete(f"/api/wishlist/{item['id']}").status_code == 404
    assert authed.patch("/api/wishlist/9999", json={"note": "x"}).status_code == 404


def test_search_results_and_listing_page_show_what_is_saved(authed):
    item = _add(authed, provider="printables", source_id="3161", title="3D BENCHY")
    found = authed.get("/api/discover/search", params={"q": "benchy", "providers": "printables,makerworld"}).json()["online"]
    saved = {(r["provider"], r["source_id"]): r["wishlist_id"] for r in found}
    assert saved[("printables", "3161")] == item["id"]
    assert saved[("printables", "9")] is None and all(v is None for (p, _), v in saved.items() if p == "makerworld")
    listing = authed.get("/api/discover/listing", params={"provider": "printables", "source_id": "3161"}).json()
    assert listing["wishlist_id"] == item["id"]


def test_download_all_queues_downloadable_items_and_reports_the_rest(authed):
    _add(authed, provider="printables", source_id="3161", title="3D BENCHY", thumbnail="https://media.printables.com/a.jpg")
    _add(authed, provider="makerworld", source_id="40146", title="Benchy Bambu")
    r = authed.post("/api/wishlist/download", json={"all": True}).json()
    queued = [i for i in r["items"] if i["id"] in r["added"]]
    assert [(i["source_id"], i["title"]) for i in queued] == [("3161", "3D BENCHY")]
    assert [(x["provider"], x["source_id"]) for x in r["rejected"]] == [("makerworld", "40146")]
    assert "browser extension" in r["rejected"][0]["reason"] and r["already_in_library"] == 0
    again = authed.post("/api/wishlist/download", json={"all": True}).json()
    assert again["added"] == []                                          # already queued: not queued twice


def test_download_chosen_ids_only(authed):
    a = _add(authed, provider="printables", source_id="3161", title="One")
    _add(authed, provider="printables", source_id="777", title="Two")
    r = authed.post("/api/wishlist/download", json={"ids": [a["id"]]}).json()
    assert [i["source_id"] for i in r["items"] if i["id"] in r["added"]] == ["3161"]


def test_download_validates_its_input(authed):
    assert authed.post("/api/wishlist/download", json={}).status_code == 400
    assert authed.post("/api/wishlist/download", json={"ids": "all"}).status_code == 400
    assert authed.post("/api/wishlist/download", json={"ids": ["1"]}).status_code == 400
    assert authed.post("/api/wishlist/download", json={"all": "yes"}).status_code == 400


def test_items_already_in_the_library_are_not_downloaded_again_and_can_be_cleared(authed):
    item = _add(authed, provider="sketchfab", source_id="a" * 32, title="Linked one")
    model = authed.post("/api/library/import", files={"file": ("wish_linked.stl", b"solid t\nfacet normal 0 0 1\n outer loop\n"
        b"  vertex 0 0 0\n  vertex 71 0 0\n  vertex 0 71 0\n endloop\nendfacet\nendsolid t\n", "application/octet-stream")}).json()
    authed.post(f"/api/library/models/{model['id']}/source", json={"provider": "sketchfab", "source_id": "a" * 32, "images": False})
    try:
        listed = authed.get("/api/wishlist").json()[0]
        assert listed["in_library"] == model["id"]
        r = authed.post("/api/wishlist/download", json={"all": True}).json()
        assert r["added"] == [] and r["already_in_library"] == 1
        assert authed.post("/api/wishlist/remove-added").json() == {"removed": 1}
        assert authed.get("/api/wishlist").json() == []
    finally:
        authed.delete(f"/api/library/models/{model['id']}/source")
        authed.delete(f"/api/library/models/{model['id']}")


def test_the_wishlist_needs_a_login(authed):
    authed.post("/api/auth/logout")
    try:
        assert authed.get("/api/wishlist").status_code == 401
        assert authed.post("/api/wishlist", json={}).status_code == 401
        assert authed.post("/api/wishlist/download", json={"all": True}).status_code == 401
    finally:
        from conftest import ensure_authenticated
        ensure_authenticated(authed)

