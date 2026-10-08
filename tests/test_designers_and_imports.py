"""Following designers, wishlist priority/status/export, and importing links and Thingiverse lists into the wishlist."""
import csv
import io

import httpx
import pytest

from app import downloads, sources
from test_sources import THINGIVERSE_TOKEN, FakeSites, SKETCHFAB_UID, _user_print


@pytest.fixture(autouse=True)
def sites(monkeypatch, authed):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(downloads, "_ensure_worker", lambda: None)
    downloads._items.clear()
    _clean(authed)
    yield fake
    _clean(authed)
    downloads._items.clear()


def _clean(c):
    for d in c.get("/api/designers").json()["designers"]:
        c.delete(f"/api/designers/{d['id']}")
    for w in c.get("/api/wishlist").json():
        c.delete(f"/api/wishlist/{w['id']}")
    c.put("/api/settings", json={"thingiverse_token": ""})


@pytest.fixture()
def token(authed):
    authed.put("/api/settings", json={"thingiverse_token": THINGIVERSE_TOKEN})


# ---------- following designers ----------

def test_results_and_listings_carry_the_designer_handle(authed):
    found = authed.get("/api/discover/search", params={"q": "benchy", "providers": "printables,sketchfab", "library": "false"}).json()["online"]
    handles = {r["provider"]: r["designer_handle"] for r in found}
    assert handles["sketchfab"] in ("jhon", "") and "printables" in handles
    listing = authed.get("/api/discover/listing", params={"provider": "sketchfab", "source_id": SKETCHFAB_UID}).json()
    assert listing["details"]["designer_handle"] == "jhon" and listing["can_follow"] is True and listing["following_id"] is None


def test_following_records_what_exists_and_then_reports_only_new_uploads(authed, sites):
    r = authed.post("/api/designers", json={"provider": "printables", "handle": "16", "name": "Prusa Research"})
    assert r.status_code == 200, r.text
    designer = r.json()
    assert designer["new_count"] == 0 and designer["last_checked_at"] and designer["label"] == "Printables"
    assert authed.get("/api/designers/uploads").json() == []                  # what was already there is not "new"

    sites.printables_user_prints = [_user_print(502, "Brand new"), _user_print(501, "Also new")] + sites.printables_user_prints
    result = authed.post("/api/designers/check", json={}).json()
    assert result["checked"] == 1 and result["new"] == 2 and result["errors"] == {} and result["new_total"] == 2
    uploads = authed.get("/api/designers/uploads").json()
    assert sorted(u["title"] for u in uploads) == ["Also new", "Brand new"]
    assert uploads[0]["designer"] == "Prusa Research" and uploads[0]["can_download"] is True and uploads[0]["in_library"] is None
    assert authed.get("/api/designers").json()["designers"][0]["new_count"] == 2

    assert authed.post("/api/designers/check", json={}).json()["new"] == 0       # nothing is reported twice
    assert authed.post("/api/designers/uploads/seen", json={"ids": [uploads[0]["id"]]}).json() == {"marked": 1}
    assert len(authed.get("/api/designers/uploads").json()) == 1
    assert authed.post("/api/designers/uploads/seen", json={"all": True}).json() == {"marked": 1}
    assert authed.get("/api/designers/uploads").json() == []
    assert len(authed.get("/api/designers/uploads", params={"unseen": "false"}).json()) == 2


def test_following_twice_returns_the_same_designer_and_unfollowing_clears_everything(authed, sites):
    a = authed.post("/api/designers", json={"provider": "printables", "handle": "16"}).json()
    b = authed.post("/api/designers", json={"provider": "printables", "handle": "16"}).json()
    assert a["id"] == b["id"] and len(authed.get("/api/designers").json()["designers"]) == 1
    sites.printables_user_prints = [_user_print(600, "Fresh")] + sites.printables_user_prints
    authed.post("/api/designers/check", json={})
    assert authed.delete(f"/api/designers/{a['id']}").status_code == 200
    assert authed.get("/api/designers/uploads", params={"unseen": "false"}).json() == []
    assert authed.delete(f"/api/designers/{a['id']}").status_code == 404


def test_sketchfab_and_thingiverse_designers(authed, sites, token):
    s = authed.post("/api/designers", json={"provider": "sketchfab", "handle": "jhon"})
    assert s.status_code == 200 and s.json()["name"] == "Jhon"
    t = authed.post("/api/designers", json={"provider": "thingiverse", "handle": "CreativeTools"})
    assert t.status_code == 200
    sites.sketchfab_user_models = [{"uid": "c" * 32, "name": "Sketch two", "user": {"username": "jhon"}, "thumbnails": {}}] + sites.sketchfab_user_models
    sites.thingiverse_user_things = [{"id": 950, "name": "Tv two", "creator": {"name": "CreativeTools"}}] + sites.thingiverse_user_things
    r = authed.post("/api/designers/check", json={}).json()
    assert r["new"] == 2
    titles = {u["title"] for u in authed.get("/api/designers/uploads").json()}
    assert titles == {"Sketch two", "Tv two"}


def test_thingiverse_designers_need_the_token(authed):
    r = authed.post("/api/designers", json={"provider": "thingiverse", "handle": "CreativeTools"})
    assert r.status_code == 502 and "token" in r.json()["detail"].lower()
    assert authed.get("/api/designers").json()["designers"] == []


@pytest.mark.parametrize("payload", [{}, {"provider": "makerworld", "handle": "x"}, {"provider": "printables", "handle": "abc"},
                                     {"provider": "sketchfab", "handle": "../x"}, {"provider": "printables", "handle": "1" * 30}])
def test_bad_designers_are_refused(authed, payload):
    assert authed.post("/api/designers", json=payload).status_code == 400


def test_a_site_outage_is_reported_and_keeps_the_known_uploads(authed, sites):
    d = authed.post("/api/designers", json={"provider": "printables", "handle": "16"}).json()
    sites.printables_down = True
    r = authed.post("/api/designers/check", json={}).json()
    assert r["checked"] == 0 and list(r["errors"]) and r["new"] == 0
    assert authed.get("/api/designers").json()["designers"][0]["last_error"]
    sites.printables_down = False
    sites.printables_user_prints = [_user_print(700, "After outage")] + sites.printables_user_prints
    assert authed.post("/api/designers/check", json={}).json()["new"] == 1
    assert authed.get("/api/designers").json()["designers"][0]["last_error"] is None
    authed.delete(f"/api/designers/{d['id']}")


def test_stale_hours_skips_recently_checked_designers(authed, sites):
    authed.post("/api/designers", json={"provider": "printables", "handle": "16"})
    assert authed.post("/api/designers/check", json={"stale_hours": 6}).json()["checked"] == 0
    assert authed.post("/api/designers/check", json={"stale_hours": 0}).json()["checked"] == 1
    assert authed.post("/api/designers/check", json={"stale_hours": "x"}).status_code == 400


def test_listing_page_knows_when_you_follow_the_designer(authed):
    authed.post("/api/designers", json={"provider": "sketchfab", "handle": "jhon"})
    listing = authed.get("/api/discover/listing", params={"provider": "sketchfab", "source_id": SKETCHFAB_UID}).json()
    assert listing["following_id"] is not None


def test_uploads_show_what_is_saved_or_in_the_library(authed, sites):
    authed.post("/api/designers", json={"provider": "printables", "handle": "16"})
    sites.printables_user_prints = [_user_print(800, "Saved one")] + sites.printables_user_prints
    authed.post("/api/designers/check", json={})
    authed.post("/api/wishlist", json={"provider": "printables", "source_id": "800", "title": "Saved one"})
    upload = authed.get("/api/designers/uploads").json()[0]
    assert upload["wishlist_id"] is not None


def test_designer_endpoints_need_a_login(authed):
    authed.post("/api/auth/logout")
    try:
        assert authed.get("/api/designers").status_code == 401
        assert authed.post("/api/designers/check", json={}).status_code == 401
    finally:
        from conftest import ensure_authenticated
        ensure_authenticated(authed)


def test_the_follow_list_is_part_of_a_backup(authed, sites):
    authed.post("/api/designers", json={"provider": "printables", "handle": "16", "name": "Prusa"})
    data = authed.get("/api/backup").content
    authed.delete(f"/api/designers/{authed.get('/api/designers').json()['designers'][0]['id']}")
    assert authed.get("/api/designers").json()["designers"] == []
    assert authed.post("/api/backup/restore", files={"file": ("b.zip", data)}, data={"confirm": "replace"}).status_code == 200
    assert [d["handle"] for d in authed.get("/api/designers").json()["designers"]] == ["16"]
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


# ---------- wishlist priority, status and export ----------

def _add(c, **fields):
    r = c.post("/api/wishlist", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


def test_priority_orders_the_list_and_high_priority_is_downloaded_first(authed):
    low = _add(authed, provider="printables", source_id="11", title="Low", priority=0)
    normal = _add(authed, provider="printables", source_id="12", title="Normal")
    high = _add(authed, provider="printables", source_id="13", title="High", priority=2)
    assert [i["title"] for i in authed.get("/api/wishlist").json()] == ["High", "Normal", "Low"]
    queued = authed.post("/api/wishlist/download", json={"all": True}).json()
    order = [i["source_id"] for i in queued["items"] if i["id"] in queued["added"]]
    assert order == ["13", "12", "11"]
    assert normal["priority"] == 1 and low["priority"] == 0 and high["priority"] == 2


def test_status_got_and_skip_are_left_out_of_add_all(authed):
    a = _add(authed, provider="printables", source_id="21", title="Wanted")
    b = _add(authed, provider="printables", source_id="22", title="Have it elsewhere", status="got")
    c = _add(authed, provider="printables", source_id="23", title="Changed my mind")
    assert authed.patch(f"/api/wishlist/{c['id']}", json={"status": "skip"}).json()["status"] == "skip"
    queued = authed.post("/api/wishlist/download", json={"all": True}).json()
    assert [i["source_id"] for i in queued["items"] if i["id"] in queued["added"]] == ["21"]
    explicit = authed.post("/api/wishlist/download", json={"ids": [b["id"]]}).json()      # asking for one by name still works
    assert [i["source_id"] for i in explicit["items"] if i["id"] in explicit["added"]] == ["22"]
    assert a["status"] == "wanted"


@pytest.mark.parametrize("payload", [{"priority": 3}, {"priority": "high"}, {"priority": True}, {"status": "maybe"}, {"status": None}])
def test_bad_priority_and_status_are_refused(authed, payload):
    item = _add(authed, provider="printables", source_id="31", title="x")
    assert authed.patch(f"/api/wishlist/{item['id']}", json=payload).status_code == 400
    assert authed.post("/api/wishlist", json={"provider": "printables", "source_id": "32", "title": "y", **payload}).status_code == 400


def test_the_wishlist_exports_as_csv(authed):
    _add(authed, provider="printables", source_id="41", title='Quote " and, comma', designer="Des", license="CC0",
         url="https://www.printables.com/model/41", note="line", priority=2)
    _add(authed, provider="makerworld", source_id="42", title="Plain", status="got")
    r = authed.get("/api/wishlist/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "wishlist.csv" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["Site", "Title", "Designer", "License", "Link", "Priority", "Status", "Note", "In library"]
    first = next(x for x in rows[1:] if x[1].startswith("Quote"))
    assert first[1] == 'Quote " and, comma' and first[0] == "Printables" and first[5] == "high" and first[7] == "line"
    assert next(x for x in rows[1:] if x[1] == "Plain")[6] == "got"


# ---------- importing links ----------

def test_pasted_links_are_added_with_their_details(authed):
    text = ("my list:\nhttps://www.printables.com/model/3161-3d-benchy and also (https://makerworld.com/en/models/40146-benchy).\n"
            "https://www.printables.com/model/3161-3d-benchy again, https://example.com/nothing")
    r = authed.post("/api/wishlist/import-links", json={"text": text}).json()
    assert r["added"] == 2 and r["already"] == 0 and r["unrecognized"] == 1 and r["failed"] == []
    titles = sorted(i["title"] for i in authed.get("/api/wishlist").json())
    assert titles == ["3D BENCHY", "Benchy Bambu Pla Basic"]
    again = authed.post("/api/wishlist/import-links", json={"text": text}).json()
    assert again["added"] == 0 and again["already"] == 2


def test_links_that_cannot_be_looked_up_are_reported(authed, sites):
    sites.makerworld_status = 404
    r = authed.post("/api/wishlist/import-links", json={"text": "https://makerworld.com/en/models/1 https://www.printables.com/model/3161"}).json()
    assert r["added"] == 1 and len(r["failed"]) == 1 and r["failed"][0]["provider"] == "makerworld"


def test_a_big_paste_is_done_in_batches(authed, monkeypatch):
    from app.routers import wishlist
    monkeypatch.setattr(wishlist, "MAX_LINKS_PER_IMPORT", 2)
    text = " ".join(f"https://www.printables.com/model/{n}" for n in (3161, 3162, 3163, 3164))
    r = authed.post("/api/wishlist/import-links", json={"text": text}).json()
    assert r["added"] == 2 and r["left_for_next_time"] == 2
    r2 = authed.post("/api/wishlist/import-links", json={"text": text}).json()
    assert r2["already"] == 2 and r2["added"] == 2


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": 5}])
def test_import_links_needs_some_text(authed, payload):
    assert authed.post("/api/wishlist/import-links", json=payload).status_code == 400


# ---------- importing from Thingiverse ----------

def test_thingiverse_likes_and_collections_import(authed, token):
    likes = authed.post("/api/wishlist/import-thingiverse", json={"kind": "likes"}).json()
    assert likes == {"found": 2, "added": 2, "already": 0}
    assert authed.post("/api/wishlist/import-thingiverse", json={"kind": "likes"}).json() == {"found": 2, "added": 0, "already": 2}
    col = authed.post("/api/wishlist/import-thingiverse",
                      json={"kind": "collection", "ref": "https://www.thingiverse.com/someone/collections/1234/my-stuff"}).json()
    assert col["added"] == 1
    assert {i["title"] for i in authed.get("/api/wishlist").json()} == {"Liked A", "Liked B", "In a collection"}


def test_thingiverse_import_needs_a_token_and_valid_input(authed):
    assert authed.post("/api/wishlist/import-thingiverse", json={"kind": "likes"}).status_code == 400
    authed.put("/api/settings", json={"thingiverse_token": THINGIVERSE_TOKEN})
    assert authed.post("/api/wishlist/import-thingiverse", json={"kind": "everything"}).status_code == 400
    assert authed.post("/api/wishlist/import-thingiverse", json={"kind": "collection", "ref": "not a link"}).status_code == 502
