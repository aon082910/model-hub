"""Downloading listings into the library (Printables and Thingiverse), with the sites faked."""
import json
import os
import time

import httpx
import pytest

from app import downloads, sources
from test_sources import THINGIVERSE_TOKEN, FakeSites, _pack_zip, _model_stl


REAL_ENSURE_WORKER = downloads._ensure_worker
TEST_LISTINGS = ("3161", "763622", "777", "888")


def _forget_downloaded_models():
    """Each test starts with none of these listings in the library."""
    from sqlmodel import Session, select
    from app.db import engine
    from app.library_cleanup import delete_model_records
    from app.models import Model3D
    with Session(engine) as session:
        ids = session.exec(select(Model3D.id).where(Model3D.source_id.in_(TEST_LISTINGS))).all()
        delete_model_records(session, ids)
        session.commit()
    # and the files they left behind, or a re-download would be renamed "deck (1).stl"
    import shutil
    from app.config import LIBRARY_PATH
    for site in ("Printables", "Thingiverse"):
        shutil.rmtree(LIBRARY_PATH / "imported" / site, ignore_errors=True)


@pytest.fixture()
def sites(monkeypatch, authed):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    # tests drive the queue by hand; the one that wants the real background worker restores it
    monkeypatch.setattr(downloads, "_ensure_worker", lambda: None)
    downloads._items.clear()
    downloads._cancel_ids.clear()
    _forget_downloaded_models()
    yield fake
    downloads._items.clear()


@pytest.fixture()
def thingiverse_token(authed):
    authed.put("/api/settings", json={"thingiverse_token": THINGIVERSE_TOKEN})
    yield
    authed.put("/api/settings", json={"thingiverse_token": ""})


def _run(listing_provider, source_id, images=True, add_tags=False, title="Test listing"):
    """Queue one listing and run it to completion in this thread."""
    downloads.enqueue([{"provider": listing_provider, "source_id": source_id, "title": title}], images=images, add_tags=add_tags)
    item = next(i for i in downloads._items if i["source_id"] == source_id and i["status"] == "queued")
    item["status"] = "downloading"
    downloads.process_item(item)
    return downloads.find_item(listing_provider, source_id)


def _models(c, ids):
    return [c.get(f"/api/library/models/{i}").json() for i in ids]


# ---------- Printables ----------

def test_printables_pack_is_imported_and_every_model_linked(authed, sites, library_path):
    item = _run("printables", "3161")
    assert item["status"] == "done", item
    assert item["message"] == "Added 2 models to the library"
    models = _models(authed, item["model_ids"])
    assert sorted(m["filename"] for m in models) == ["deck.stl", "hull.stl"]          # readme and picture were left behind
    for m in models:
        assert m["path"].replace("\\", "/").startswith("imported/Printables/3D BENCHY [3161]/")
        assert (m["source_provider"], m["source_id"], m["source_linked_by"]) == ("printables", "3161", "download")
        assert m["designer"] == "Prusa Research" and m["license"] == "CC0"
        assert m["source_url"].startswith("https://www.printables.com/model/3161")
        assert json.loads(m["source_images"])                      # pictures for every model, downloaded once and copied
    folder = os.path.join(library_path, "imported", "Printables", "3D BENCHY [3161]")
    assert sorted(os.listdir(folder)) == ["deck.stl", "hull.stl"]
    assert not os.path.exists(downloads.TEMP_ROOT / item["id"])    # temporary download removed
    pack_requests = [r for r in sites.file_requests if r.endswith(".zip")]
    assert len(pack_requests) == 1                                 # the pack, not each file separately


def test_printables_without_a_pack_uses_the_individual_stls(authed, sites):
    sites.printables_files = {"data": {"print": {"id": "3161", "downloadPacks": [], "stls": [
        {"id": "49068", "name": "3dbenchy.stl", "fileSize": 10}]}}}
    item = _run("printables", "3161")
    assert item["status"] == "done"
    assert [m["filename"] for m in _models(authed, item["model_ids"])] == ["3dbenchy.stl"]


def test_a_listing_already_in_the_library_is_skipped(authed, sites):
    first = _run("printables", "3161")
    assert first["status"] == "done"
    again = _run("printables", "3161")
    assert again["status"] == "skipped" and again["message"] == "Already in your library"
    assert sorted(again["model_ids"]) == sorted(first["model_ids"])


def test_images_and_tags_options(authed, sites):
    item = _run("printables", "3161", images=False, add_tags=True)
    model = _models(authed, item["model_ids"])[0]
    assert model["source_images"] is None
    assert {t["name"] for t in model["tags"]} >= {"benchy", "boat"}


def test_listing_with_only_non_model_files_is_an_error(authed, sites):
    import zipfile, io
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("readme.txt", "hi")
    sites.printables_zip = out.getvalue()
    item = _run("printables", "3161")
    assert item["status"] == "error" and "No model files" in item["message"]
    assert not os.path.exists(downloads.TEMP_ROOT / item["id"])


# ---------- safety ----------

@pytest.mark.parametrize("link,why", [
    ("https://evil.example.com/media/x.zip", "unexpected site"),
    ("http://files.printables.com/media/x.zip", "unexpected site"),
    ("https://files.printables.com.evil.example.com/x.zip", "unexpected site"),
])
def test_download_links_outside_the_sites_own_domain_are_refused(authed, sites, link, why):
    sites.printables_link = link
    item = _run("printables", "3161")
    assert item["status"] == "error" and why in item["message"]
    assert sites.file_requests == []                                # nothing was fetched


def test_thingiverse_token_goes_only_to_thingiverse_hosts(authed, sites, thingiverse_token):
    item = _run("thingiverse", "763622")
    assert item["status"] == "done", item
    assert [m["filename"] for m in _models(authed, item["model_ids"])] == ["benchy.stl"]      # not the txt or the jpg
    assert sites.thingiverse_download_auth == [f"Bearer {THINGIVERSE_TOKEN}"]                 # sent to www.thingiverse.com
    assert sites.cdn_download_auth == [None]                                                  # never to the CDN it redirected to


def test_a_redirect_to_another_site_is_refused(authed, sites, thingiverse_token):
    sites.thingiverse_redirect_to = "https://evil.example.com/steal.stl"
    item = _run("thingiverse", "763622")
    assert item["status"] == "error" and "unexpected address" in item["message"]


def test_a_redirect_over_plain_http_is_refused(authed, sites, thingiverse_token):
    sites.thingiverse_redirect_to = "http://cdn.thingiverse.com/files/1/benchy.stl"
    assert "unexpected address" in _run("thingiverse", "763622")["message"]


def test_size_limit(authed, sites, monkeypatch):
    monkeypatch.setattr(downloads, "MAX_DOWNLOAD_BYTES", 100)
    item = _run("printables", "3161")
    assert item["status"] == "error" and "larger than" in item["message"]


@pytest.mark.parametrize("status,fragment", [(403, "refused the download"), (404, "not found"), (429, "rate limiting"), (500, r"failed \(500\)")])
def test_download_errors_become_messages(authed, sites, status, fragment):
    sites.printables_file_status = status
    item = _run("printables", "3161")
    assert item["status"] == "error"
    import re
    assert re.search(fragment, item["message"])


def test_filenames_from_the_site_are_made_safe():
    assert downloads.safe_name("../../etc/passwd") == "etcpasswd"
    assert downloads.safe_name("A very: bad* <name>?") == "A very bad name"
    assert downloads.safe_name("") == "listing" and downloads.safe_name("...") == "listing"
    assert downloads.safe_name("x" * 300) == "x" * 80


def test_file_plans():
    files = [{"kind": "pack", "name": "a.zip"}, {"kind": "stl", "name": "b.stl"}, {"kind": "stl", "name": "c.stl"}]
    assert [f["name"] for f in downloads.plan_printables_files(files)] == ["a.zip"]
    assert [f["name"] for f in downloads.plan_printables_files(files[1:])] == ["b.stl", "c.stl"]
    thing = [{"name": "x.stl"}, {"name": "notes.txt"}, {"name": "all.zip"}, {"name": "y.3MF"}]
    assert [f["name"] for f in downloads.plan_thingiverse_files(thing)] == ["x.stl", "y.3MF"]
    assert [f["name"] for f in downloads.plan_thingiverse_files(thing[1:3])] == ["all.zip"]
    assert downloads.plan_thingiverse_files([{"name": "readme.txt"}]) == []


# ---------- the queue and the API ----------

def _wait_until_idle(c, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = c.get("/api/discover/downloads").json()
        if not status["running"] and not any(i["status"] in ("queued", "downloading", "importing") for i in status["items"]):
            return status
        time.sleep(0.1)
    raise AssertionError("downloads did not finish")


def test_queue_through_the_api_with_a_background_worker(authed, sites, monkeypatch):
    monkeypatch.setattr(downloads, "_ensure_worker", REAL_ENSURE_WORKER)
    r = authed.post("/api/discover/downloads", json={"items": [
        {"provider": "printables", "source_id": "3161", "title": "3D BENCHY", "thumbnail": "https://media.printables.com/x.jpg"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["added"]) == 1 and body["rejected"] == []
    status = _wait_until_idle(authed)
    item = status["items"][0]
    assert item["status"] == "done" and item["title"] == "3D BENCHY" and len(item["model_ids"]) == 2
    assert "images" not in item and "add_tags" not in item         # internal options are not exposed
    assert authed.post("/api/discover/downloads/clear").json()["cleared"] == 1
    assert authed.get("/api/discover/downloads").json()["items"] == []


def test_sites_that_cannot_be_downloaded_from_are_rejected_with_the_reason(authed, sites):
    r = authed.post("/api/discover/downloads", json={"items": [
        {"provider": "makerworld", "source_id": "40146"},
        {"provider": "sketchfab", "source_id": "a" * 32},
        {"provider": "cults3d", "source_id": "some-slug"},
        {"provider": "myminifactory", "source_id": "11323"},
        {"provider": "thingiverse", "source_id": "763622"},               # no token saved
        {"provider": "printables", "source_id": "not-a-number"},
        {"provider": "nowhere", "source_id": "1"},
    ]}).json()
    assert r["added"] == []
    reasons = {(x["provider"], x["source_id"]): x["reason"] for x in r["rejected"]}
    assert "browser extension" in reasons[("makerworld", "40146")]
    assert "glTF" in reasons[("sketchfab", "a" * 32)]
    assert "never serves model files" in reasons[("cults3d", "some-slug")]
    assert "browser extension" in reasons[("myminifactory", "11323")]
    assert "credentials in Settings" in reasons[("thingiverse", "763622")]
    assert reasons[("printables", "not-a-number")] == "Not a valid listing"
    assert reasons[("nowhere", "1")] == "Not a valid listing"
    assert sites.file_requests == []


def test_queue_input_validation(authed, sites):
    for bad in ({}, {"items": []}, {"items": "x"}):
        assert authed.post("/api/discover/downloads", json=bad).status_code == 400
    too_many = [{"provider": "printables", "source_id": str(i)} for i in range(101)]
    assert authed.post("/api/discover/downloads", json={"items": too_many}).status_code == 400


def test_the_same_listing_is_not_queued_twice(authed, sites):
    first = downloads.enqueue([{"provider": "printables", "source_id": "777"}])
    again = downloads.enqueue([{"provider": "printables", "source_id": "777"}])
    assert len(first) == 1 and again == []
    downloads._items[:] = []


def test_cancel_a_waiting_item(authed, sites, monkeypatch):
    monkeypatch.setattr(downloads, "_ensure_worker", lambda: None)         # keep it waiting
    added = downloads.enqueue([{"provider": "printables", "source_id": "888"}])
    assert authed.delete(f"/api/discover/downloads/{added[0]}").status_code == 200
    assert downloads.find_item("printables", "888")["status"] == "cancelled"
    assert authed.delete(f"/api/discover/downloads/{added[0]}").status_code == 404      # nothing left to cancel
    assert authed.delete("/api/discover/downloads/nope").status_code == 404
    downloads._items[:] = []


def test_cancelling_a_running_download_stops_it(authed, sites):
    downloads.enqueue([{"provider": "printables", "source_id": "3161"}])
    item = next(i for i in downloads._items if i["status"] == "queued")
    item["status"] = "downloading"
    downloads._cancel_ids.add(item["id"])                      # as if the user pressed cancel during the download
    downloads.process_item(item)
    assert item["status"] == "cancelled"
    assert not os.path.exists(downloads.TEMP_ROOT / item["id"])
    downloads._items[:] = []


def test_downloads_need_a_login(client):
    client.post("/api/auth/logout")
    assert client.get("/api/discover/downloads").status_code == 401
    assert client.post("/api/discover/downloads", json={"items": []}).status_code == 401
    from conftest import ensure_authenticated
    ensure_authenticated(client)
