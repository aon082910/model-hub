"""Thingi10K and Objaverse as sources, with Hugging Face and Sketchfab faked."""
import gzip
import json
import shutil

import httpx
import pytest
from starlette.testclient import TestClient

from app import dataset_sources, downloads, sources

PW = "a long enough password"
UID1, UID2, UID3 = "a" * 32, "b" * 32, "c" * 32

CONTEXTUAL = ("Thing ID,Date,Category,Sub-category,Name,Author,License\n"
              "10367,2011-07-26T09:23:59,None,None,Octocat ,BrianEnigma,Creative Commons - Attribution - Share Alike\n"
              "10955,2011-08-22T17:52:48,Mechanical Parts,Gears,Spiral bevel gear ,GeneralRulofDumb,Creative Commons - Attribution - Non-Commercial\n"
              "11322,2011-09-08T13:31:25,None,None,Coin Sorter,HPaul,Public Domain\n"
              "99999,2012-01-01T00:00:00,None,None,Has no files,Nobody,Creative Commons - Attribution\n")
TAGS = "Thing ID,Tag\n10367,cat\n10367,3D\n10955,gear\n10955,Gear\n10955,mechanical\n"
SUMMARY = ("ID,Thing ID,License,Link,No duplicated faces,Closed\n"
           "32770,10367,Creative Commons - Attribution - Share Alike,https://thingiverse-production-new.s3.amazonaws.com/assets/00/Octocat-v1.stl,TRUE,TRUE\n"
           "34783,10955,Creative Commons - Attribution - Non-Commercial,https://thingiverse-production-new.s3.amazonaws.com/assets/08/gear_left.stl,TRUE,FALSE\n"
           "34784,10955,Creative Commons - Attribution - Non-Commercial,https://thingiverse-production-new.s3.amazonaws.com/assets/e8/gear_right.stl,TRUE,TRUE\n"
           "50001,11322,Public Domain,https://example.com/x/sorter,TRUE,TRUE\n")


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


def _glb(extent=1.0):
    import trimesh
    return trimesh.creation.box(extents=(extent, extent * 2, extent * 3)).export(file_type="glb")


LVIS = {"chair": [UID1, UID2], "teddy_bear": [UID3], "empty_one": []}
PATHS = {UID1: f"glbs/000-001/{UID1}.glb", UID2: f"glbs/000-002/{UID2}.glb", UID3: f"glbs/000-003/{UID3}.glb", "d" * 32: "glbs/000-009/" + "d" * 32 + ".glb"}
SKETCHFAB = {UID1: {"uid": UID1, "name": "Wooden chair", "user": {"displayName": "Carla", "username": "carla"}, "license": {"label": "CC Attribution"},
                    "thumbnails": {"images": [{"url": "https://media.sketchfab.com/c.jpg", "width": 1600}]}, "tags": [{"name": "furniture"}], "viewerUrl": f"https://sketchfab.com/3d-models/chair-{UID1}",
                    "description": "<p>A chair.</p>", "categories": [{"name": "Furniture"}]},
             UID2: {"uid": UID2, "name": "Plastic chair", "user": {"username": "dave"}, "license": {"label": "CC Attribution-NonCommercial"}, "thumbnails": {"images": []}}}


class FakeHub:
    def __init__(self):
        self.requests, self.glb, self.status = [], _glb(), 200
        self.redirect_to = None

    def __call__(self, request):
        host, path = request.url.host, request.url.path
        self.requests.append(f"{host}{path}")
        if host == "huggingface.co":
            if self.status != 200:
                return httpx.Response(self.status)
            body = {"/datasets/Thingi10K/Thingi10K/resolve/main/metadata/contextual_data.csv": CONTEXTUAL, "/datasets/Thingi10K/Thingi10K/resolve/main/metadata/tag_data.csv": TAGS,
                    "/datasets/Thingi10K/Thingi10K/resolve/main/metadata/input_summary.csv": SUMMARY}.get(path)
            if body is not None:
                return httpx.Response(200, text=body)
            if path.endswith("lvis-annotations.json.gz"):
                return httpx.Response(200, content=gzip.compress(json.dumps(LVIS).encode()))
            if path.endswith("object-paths.json.gz"):
                return httpx.Response(200, content=gzip.compress(json.dumps(PATHS).encode()))
            if "/raw_meshes/" in path or path.endswith(".glb"):
                return httpx.Response(302, headers={"location": self.redirect_to or "https://cas-bridge.xethub.hf.co/xet/" + path.rsplit("/", 1)[-1]})
        if host == "cas-bridge.xethub.hf.co":
            return httpx.Response(200, content=self.glb if path.endswith(".glb") else _stl(33), headers={"content-disposition": f'inline; filename="{path.rsplit("/", 1)[-1]}"'})
        if host == "evil.example":
            return httpx.Response(200, content=_stl(5))
        if host == "api.sketchfab.com" and path.startswith("/v3/models/"):
            uid = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=SKETCHFAB[uid]) if uid in SKETCHFAB else httpx.Response(404)
        return httpx.Response(404)


@pytest.fixture()
def hub(monkeypatch, authed):
    fake = FakeHub()
    monkeypatch.setattr(dataset_sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(downloads, "_ensure_worker", lambda: None)
    downloads._items.clear()
    for name in dataset_sources.NAMES:
        dataset_sources.remove(name)
    yield fake
    downloads._items.clear()
    for name in dataset_sources.NAMES:
        dataset_sources.remove(name)
    _forget()


def _forget():
    from sqlmodel import Session, select
    from app.config import LIBRARY_PATH
    from app.db import engine
    from app.library_cleanup import delete_model_records
    from app.models import Model3D
    with Session(engine) as session:
        delete_model_records(session, session.exec(select(Model3D.id).where(Model3D.source_provider.in_(["thingi10k", "objaverse"]))).all())
        session.commit()
    for label in ("Thingi10K (research dataset)", "Objaverse (research dataset)"):
        shutil.rmtree(LIBRARY_PATH / "imported" / label, ignore_errors=True)


def _run(provider, source_id):
    downloads.enqueue([{"provider": provider, "source_id": source_id, "title": "x"}], images=False, add_tags=True)
    item = next(i for i in downloads._items if i["source_id"] == source_id and i["status"] == "queued")
    item["status"] = "downloading"
    downloads.process_item(item)
    return downloads.find_item(provider, source_id)


# ---------------------------------------------------------------- the lists
def test_licence_names_are_made_short():
    n = dataset_sources.normalise_license
    assert n("Creative Commons - Attribution - Share Alike") == "CC BY-SA" and n("Creative Commons - Attribution") == "CC BY"
    assert n("Creative Commons - Attribution - Non-Commercial") == "CC BY-NC" and n("Creative Commons - Attribution - Non-Commercial - Share Alike") == "CC BY-NC-SA"
    assert n("Creative Commons - Attribution - No Derivatives") == "CC BY-ND" and n("Creative Commons - Public Domain Dedication") == "CC0" and n("Public Domain") == "CC0"
    assert n("GNU - GPL") == "GPL" and n("GNU - LGPL") == "LGPL" and n("BSD License") == "BSD" and n("None") == "" and n(None) == "" and n("Something else") == "Something else"


def test_a_dataset_takes_no_part_in_searches_until_its_list_is_downloaded(authed, hub):
    assert authed.get("/api/settings/datasets").json() == {"thingi10k": {"present": False, "items": 0, "age_days": None}, "objaverse": {"present": False, "items": 0, "age_days": None}}
    providers = {p["id"]: p for p in authed.get("/api/sources/providers").json()}
    assert providers["thingi10k"]["enabled"] is False and providers["objaverse"]["enabled"] is False and providers["thingi10k"]["can_download"] is True
    assert "thingi10k" not in sources.available_providers() and "archive" in sources.available_providers()
    assert authed.get("/api/discover/search", params={"q": "gear", "providers": "thingi10k"}).json()["errors"]["thingi10k"].startswith("Download the Thingi10K list first")
    s = authed.post("/api/settings/datasets/thingi10k/update").json()
    assert s == {"present": True, "items": 3, "age_days": 0.0}                                                           # the thing with no files is left out
    assert "thingi10k" in sources.available_providers() and "objaverse" not in sources.available_providers()
    assert authed.post("/api/settings/datasets/nonsense/update").status_code == 404
    assert authed.delete("/api/settings/datasets/thingi10k").json()["thingi10k"]["present"] is False


def test_the_lists_come_only_from_hugging_face_and_a_failure_changes_nothing(authed, hub):
    authed.post("/api/settings/datasets/thingi10k/update")
    assert {r.split("/")[0] for r in hub.requests} == {"huggingface.co"}
    hub.status = 500
    r = authed.post("/api/settings/datasets/thingi10k/update")
    assert r.status_code == 502 and "did not give the file" in r.json()["detail"]
    assert authed.get("/api/settings/datasets").json()["thingi10k"]["present"] is True                                    # the old list is kept
    hub.status = 200
    hub.redirect_to = "https://evil.example/x"
    with pytest.raises(dataset_sources.DatasetError):
        dataset_sources.fetch("https://huggingface.co/datasets/Thingi10K/Thingi10K/resolve/main/raw_meshes/1.stl", 1000)
    with pytest.raises(dataset_sources.DatasetError):
        dataset_sources.fetch("http://huggingface.co/x", 1000)                                                           # not https


# ---------------------------------------------------------------- Thingi10K
def test_things_are_searched_by_name_author_tag_and_category(authed, hub):
    authed.post("/api/settings/datasets/thingi10k/update")
    r = authed.get("/api/discover/search", params={"q": "gear", "providers": "thingi10k", "library": "false"}).json()
    assert r["errors"] == {} and [x["title"] for x in r["online"]] == ["Spiral bevel gear"]
    gear = r["online"][0]
    assert (gear["provider"], gear["source_id"], gear["designer"], gear["license"], gear["can_download"]) == ("thingi10k", "10955", "GeneralRulofDumb", "CC BY-NC", True)
    assert gear["url"] == "https://www.thingiverse.com/thing:10955"
    for q, titles in (("octocat 3d", ["Octocat"]), ("mechanical gears", ["Spiral bevel gear"]), ("brianenigma", ["Octocat"]), ("zzz", []), ("coin", ["Coin Sorter"])):
        got = authed.get("/api/discover/search", params={"q": q, "providers": "thingi10k", "library": "false"}).json()
        assert [x["title"] for x in got["online"]] == titles, q
    paged = authed.get("/api/discover/search", params={"q": "e", "providers": "thingi10k", "limit": 1, "page": 2, "library": "false"}).json()
    assert len(paged["online"]) == 1


def test_a_thing_has_details_and_its_own_stl_files(authed, hub):
    authed.post("/api/settings/datasets/thingi10k/update")
    listing = authed.get("/api/discover/listing", params={"provider": "thingi10k", "source_id": "10955"}).json()
    d = listing["details"]
    assert d["title"] == "Spiral bevel gear" and d["license"] == "CC BY-NC" and d["tags"] == ["gear", "mechanical"] and d["category"] == "Mechanical Parts / Gears"
    assert "licence" in d["description"] and listing["can_download"] is True
    assert [(f["id"], f["name"], f["selected"]) for f in listing["files"]] == [("34783", "gear_left.stl", True), ("34784", "gear_right.stl", True)]
    assert authed.get("/api/discover/listing", params={"provider": "thingi10k", "source_id": "424242"}).status_code == 502


def test_a_thing_is_downloaded_from_hugging_faces_own_servers(authed, hub):
    authed.post("/api/settings/datasets/thingi10k/update")
    item = _run("thingi10k", "10955")
    assert item["status"] == "done", item["message"]
    models = [authed.get(f"/api/library/models/{i}").json() for i in item["model_ids"]]
    assert sorted(m["filename"] for m in models) == ["34783.stl", "34784.stl"]
    m = models[0]
    assert (m["source_provider"], m["source_id"], m["designer"], m["license"]) == ("thingi10k", "10955", "GeneralRulofDumb", "CC BY-NC")
    assert {t["name"] for t in m["tags"]} >= {"gear"} and m["path"].replace("\\", "/").startswith("imported/Thingi10K (research dataset)/Spiral bevel gear [10955]/")
    assert {r.split("/")[0] for r in hub.requests if "raw_meshes" in r or "xethub" in r} <= {"huggingface.co", "cas-bridge.xethub.hf.co"}


def test_a_redirect_away_from_hugging_face_is_refused(authed, hub):
    authed.post("/api/settings/datasets/thingi10k/update")
    hub.redirect_to = "https://evil.example/x.stl"
    item = _run("thingi10k", "10367")
    assert item["status"] == "error" and not any(r.startswith("evil.example") for r in hub.requests)


# ---------------------------------------------------------------- Objaverse
def test_objaverse_is_searched_by_category_and_named_by_sketchfab(authed, hub):
    s = authed.post("/api/settings/datasets/objaverse/update").json()
    assert s["present"] is True and s["items"] == 3                                                                      # empty categories and objects without a path are dropped
    r = authed.get("/api/discover/search", params={"q": "chair", "providers": "objaverse", "library": "false"}).json()
    assert r["errors"] == {} and sorted(x["title"] for x in r["online"]) == ["Plastic chair", "Wooden chair"]
    wooden = next(x for x in r["online"] if x["title"] == "Wooden chair")
    assert (wooden["provider"], wooden["source_id"], wooden["designer"], wooden["license"], wooden["thumbnail"]) == ("objaverse", UID1, "Carla", "CC Attribution", "https://media.sketchfab.com/c.jpg")
    nc = next(x for x in r["online"] if x["title"] == "Plastic chair")
    assert nc["license"] == "CC Attribution-NonCommercial"                                                               # the restriction is on show
    bear = authed.get("/api/discover/search", params={"q": "teddy bear", "providers": "objaverse", "library": "false"}).json()["online"]
    assert len(bear) == 1 and bear[0]["title"].startswith("Teddy bear (") and bear[0]["license"].startswith("Creative Commons")                # Sketchfab no longer has it
    by_id = authed.get("/api/discover/search", params={"q": UID2, "providers": "objaverse", "library": "false"}).json()["online"]
    assert [x["source_id"] for x in by_id] == [UID2]
    assert authed.get("/api/discover/search", params={"q": "spaceship", "providers": "objaverse", "library": "false"}).json()["online"] == []
    assert {x.split("/")[0] for x in hub.requests} <= {"huggingface.co", "api.sketchfab.com"}


def test_an_objaverse_object_is_downloaded_converted_and_scaled_to_print(authed, hub):
    authed.post("/api/settings/datasets/objaverse/update")
    listing = authed.get("/api/discover/listing", params={"provider": "objaverse", "source_id": UID1}).json()
    assert listing["details"]["license"] == "CC Attribution" and "80 mm" in listing["details"]["description"] and listing["files"][0]["name"] == f"{UID1}.glb"
    item = _run("objaverse", UID1)
    assert item["status"] == "done", item["message"]
    m = authed.get(f"/api/library/models/{item['model_ids'][0]}").json()
    assert m["filename"].endswith(".stl") and (m["source_provider"], m["source_id"], m["designer"]) == ("objaverse", UID1, "Carla")
    dims = sorted([m["bbox_x"], m["bbox_y"], m["bbox_z"]])
    assert dims[2] == pytest.approx(80.0, abs=0.5) and dims[0] == pytest.approx(80.0 / 3, abs=0.5)                       # the longest side is 80 mm, proportions kept
    assert not list(downloads.TEMP_ROOT.glob("*/*.glb"))                                                                  # the GLB is not left behind


def test_a_model_that_cannot_be_converted_is_reported_not_imported(authed, hub):
    hub.glb = b"this is not a glb file at all"
    authed.post("/api/settings/datasets/objaverse/update")
    item = _run("objaverse", UID2)
    assert item["status"] == "error" and "converted" in item["message"]
    assert not list(downloads.TEMP_ROOT.glob("*/*.glb"))


def test_only_the_administrator_may_manage_the_lists(authed, hub):
    from app.main import app
    authed.post("/api/users", json={"username": "dsmember", "password": PW, "role": "member"})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "dsmember", "password": PW})
        assert member.get("/api/settings/datasets").status_code == 403 and member.post("/api/settings/datasets/thingi10k/update").status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


def test_the_lists_count_in_the_storage_overview(authed, hub):
    authed.post("/api/settings/datasets/thingi10k/update")
    assert "datasets" in {f["name"] for f in authed.get("/api/storage").json()["folders"]}
    assert next(f for f in authed.get("/api/storage").json()["folders"] if f["name"] == "datasets")["bytes"] > 0


def test_the_controls_are_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="datasets-box"' in html
    for needle in ("/api/settings/datasets", "loadDatasets", "thingi10k: 'Thingi10K", "objaverse: 'Objaverse"):
        assert needle in js, needle
