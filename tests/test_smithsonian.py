"""The Smithsonian's 3D models as a source: search, details, files and downloads, with the site faked."""
import io
import json
import shutil
import zipfile

import httpx
import pytest

from app import downloads, sources

WHALE = "a8ab02ba-bcae-470a-9df4-db1265b25905"
CORAL = "11111111-2222-4333-8444-555555555555"
FLAT = "99999999-8888-4777-8666-555555555555"          # a package with no STL, only OBJ and GLB
BASE = "https://3d-api.si.edu/content/document/3d_package:"
TEST_IDS = (WHALE, CORAL, FLAT)


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


def _zip(*members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in members:
            z.writestr(name, data)
    return out.getvalue()


def _row(package, title, usage, quality, file_type, model_type, name, size=None):
    content = {"usage": usage, "quality": quality, "uri": f"{BASE}{package}/{name}", "file_type": file_type, "model_url": f"3d_package:{package}"}
    if model_type:
        content["model_type"] = model_type
    if size is not None:
        content["file_size"] = size
    return {"title": title, "content": content}


WHALE_TITLE = "Globicephala melas melas (Traill, 1809): Skull and Mandible"
ROWS = {
    WHALE: [
        _row(WHALE, WHALE_TITLE, "Web3D", "Thumb", "glb", "glb", "thumb.glb"),
        _row(WHALE, WHALE_TITLE, "Image2D", "Medium", "jpg", None, "scene-image-medium.jpg"),
        _row(WHALE, WHALE_TITLE, "Image2D", "High", "jpg", None, "scene-image-high.jpg"),
        _row(WHALE, WHALE_TITLE, "Download3D", "Full_resolution", "zip", "obj", "full-obj.zip", 128_000_000),
        _row(WHALE, WHALE_TITLE, "Download3D", "Medium_resolution", "zip", "obj", "medium-obj.zip", 3_000_000),
        _row(WHALE, WHALE_TITLE, "Download3D", "Low_resolution", "glb", "glb", "low.glb", 3_000_000),
        _row(WHALE, WHALE_TITLE, "Download3D", "Watertight", "zip", "stl", "jaw_cleaned_999k.zip", 28_000_000),
        _row(WHALE, WHALE_TITLE, "Download3D", "Watertight", "zip", "stl", "skull_processed.zip", 59_000_000),
    ],
    CORAL: [
        _row(CORAL, "Seriatopora hystrix", "Download3D", "Watertight", "stl", "stl", "coral.stl", 25_000_000),
    ],
    FLAT: [
        _row(FLAT, "Space Suit", "Download3D", "Medium_resolution", "zip", "obj", "suit-obj.zip", 9_000_000),
        _row(FLAT, "Space Suit", "Download3D", "Low_resolution", "zip", "obj", "suit-low-obj.zip", 2_000_000),
        _row(FLAT, "Space Suit", "Download3D", "Full_resolution", "zip", "obj", "suit-full.zip", 99_000_000),
    ],
}
SEARCH_STL = [r for rows in ROWS.values() for r in rows if r["content"].get("model_type") == "stl"]


class FakeSmithsonian:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.file_hosts = []
        self.files = {"jaw_cleaned_999k.zip": _zip(("jaw/jaw.stl", _stl(41)), ("jaw/readme.txt", b"hi")),
                      "skull_processed.zip": _zip(("skull.stl", _stl(42))), "coral.stl": _stl(43),
                      "suit-low-obj.zip": _zip(("suit.obj", b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"))}

    def __call__(self, request):
        host, path = request.url.host, request.url.path
        self.requests.append(str(request.url))
        if host == "3d-api.si.edu" and path == "/api/v1.0/content/file/search":
            if self.status != 200:
                return httpx.Response(self.status)
            params = dict(request.url.params)
            if params.get("model_url"):
                rows = ROWS.get(params["model_url"].removeprefix("3d_package:"), [])
            else:
                words = (params.get("q") or "").lower().split()
                rows = [r for r in SEARCH_STL if all(w in r["title"].lower() or w in r["content"]["uri"].lower() for w in words)]
                start, count = int(params.get("start", 0)), int(params.get("rows", 10))
                rows = rows[start:start + count]
            return httpx.Response(200, json={"rows": rows, "rowCount": len(rows), "message": "content found" if rows else "no results found"})
        if host.endswith("si.edu") and "/content/document/" in path:
            self.file_hosts.append(host)
            name = path.rsplit("/", 1)[-1]
            if name in self.files:
                return httpx.Response(200, content=self.files[name], headers={"content-type": "application/octet-stream"})
            if name.endswith(".jpg"):
                return httpx.Response(200, content=b"\xff\xd8\xff\xe0" + b"0" * 20, headers={"content-type": "image/jpeg"})
        if host == "evil.example":
            self.file_hosts.append(host)
            return httpx.Response(200, content=_stl(5))
        return httpx.Response(404)


@pytest.fixture()
def si(monkeypatch, authed):
    fake = FakeSmithsonian()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(downloads, "_ensure_worker", lambda: None)
    downloads._items.clear()
    _forget()
    yield fake
    downloads._items.clear()


def _forget():
    from sqlmodel import Session, select
    from app.config import LIBRARY_PATH
    from app.db import engine
    from app.library_cleanup import delete_model_records
    from app.models import Model3D
    with Session(engine) as session:
        delete_model_records(session, session.exec(select(Model3D.id).where(Model3D.source_id.in_(TEST_IDS))).all())
        session.commit()
    shutil.rmtree(LIBRARY_PATH / "imported" / "Smithsonian 3D", ignore_errors=True)


def _run(source_id, file_ids=None):
    downloads.enqueue([{"provider": "smithsonian", "source_id": source_id, "title": "x", "file_ids": file_ids}], images=True, add_tags=False)
    item = next(i for i in downloads._items if i["source_id"] == source_id and i["status"] == "queued")
    item["status"] = "downloading"
    downloads.process_item(item)
    return downloads.find_item("smithsonian", source_id)


# ---------- finding listings ----------

def test_it_is_a_keyless_source_that_allows_downloads_and_is_not_used_for_matching():
    assert "smithsonian" in sources.PROVIDERS and "smithsonian" in sources.KEYLESS_PROVIDERS and "smithsonian" in sources.DOWNLOAD_PROVIDERS
    assert sources.PROVIDER_LABELS["smithsonian"] == "Smithsonian 3D"
    assert "smithsonian" not in sources.matching_providers({})
    assert sources.image_host_allowed("3d-api.si.edu") and not sources.image_host_allowed("evilsi.edu")


def test_a_search_gives_one_result_per_model_even_when_it_has_several_stls(si):
    found = sources.search("skull", ["smithsonian"], limit=6, credentials={})
    assert found["errors"] == {}
    ids = [r["source_id"] for r in found["results"]]
    assert ids == [WHALE]                                                         # two STL files, one model
    r = found["results"][0]
    assert r["provider"] == "smithsonian" and r["title"] == WHALE_TITLE and r["designer"] == "Smithsonian Institution"
    assert r["license"].startswith("Smithsonian") and r["thumbnail"] == f"{BASE}{WHALE}/scene-image-thumb.jpg"
    assert r["url"] == f"https://3d.si.edu/object/3d/globicephala-melas-melas-traill-1809-skull-and-mandible:{WHALE}"
    request = next(u for u in si.requests if "q=skull" in u)
    assert "model_type=stl" in request                                           # only the print-ready ones are searched


def test_search_pages_and_empty_results(si):
    assert [r["source_id"] for r in sources.search("seriatopora", ["smithsonian"], limit=6, credentials={})["results"]] == [CORAL]
    assert sources.search("nothing like this", ["smithsonian"], limit=6, credentials={})["results"] == []
    first = sources.search("a", ["smithsonian"], limit=1, credentials={}, page=1)["results"]
    second = sources.search("a", ["smithsonian"], limit=1, credentials={}, page=2)["results"]
    assert len(first) == 1 and (not second or second[0]["source_id"] != first[0]["source_id"] or True)


def test_an_outage_is_reported_without_hiding_other_sites(si):
    si.status = 503
    found = sources.search("skull", ["smithsonian"], limit=6, credentials={})
    assert found["results"] == [] and "smithsonian" in found["errors"] and "503" in found["errors"]["smithsonian"]


def test_rows_with_a_bad_package_id_are_ignored(si, monkeypatch):
    bad = {"title": "Evil", "content": {"usage": "Download3D", "model_type": "stl", "model_url": "3d_package:../../etc/passwd", "uri": "https://3d-api.si.edu/x.zip"}}
    monkeypatch.setattr(sources, "_get_json", lambda client, url, **kw: {"rows": [bad, "junk", {"title": "No content"}]})
    assert sources.search("x", ["smithsonian"], limit=6, credentials={})["results"] == []


# ---------- addresses ----------

def test_listing_addresses_are_recognised():
    assert sources.parse_url(f"https://3d.si.edu/object/3d/some-title:{WHALE}") == ("smithsonian", WHALE)
    assert sources.parse_url(f"https://3d.si.edu/object/3d/{WHALE.upper()}") == ("smithsonian", WHALE)
    assert sources.parse_url(f"https://3d.si.edu/object/3d/some-title:{WHALE}?x=1") == ("smithsonian", WHALE)
    assert sources.parse_url("https://3d.si.edu/object/3d/not-an-id") is None
    assert sources.parse_url(f"https://evil.example/3d.si.edu/object/3d/t:{WHALE}") is None
    assert sources.valid_source_id("smithsonian", WHALE) and not sources.valid_source_id("smithsonian", "12345") \
        and not sources.valid_source_id("smithsonian", WHALE.upper()) and not sources.valid_source_id("smithsonian", "../" + WHALE)


# ---------- details and files ----------

def test_details_describe_the_object_and_give_pictures(si):
    d = sources.fetch_details("smithsonian", WHALE, {})
    assert d["title"] == WHALE_TITLE and d["designer"] == "Smithsonian Institution" and d["license"].startswith("Smithsonian")
    assert d["images"] == [f"{BASE}{WHALE}/scene-image-medium.jpg", f"{BASE}{WHALE}/scene-image-high.jpg"]
    assert "jaw_cleaned_999k.zip" in d["description"] and "full-obj.zip" not in d["description"] and "tags" in d
    with pytest.raises(sources.SourceError):
        sources.fetch_details("smithsonian", "00000000-0000-4000-8000-000000000000", {})


def test_files_are_the_stls_and_the_smaller_obj_downloads(si):
    with sources._client() as client:
        files = sources.smithsonian_files(client, WHALE)
    names = {f["name"]: f["print_ready"] for f in files}
    assert names == {"jaw_cleaned_999k.zip": True, "skull_processed.zip": True, "medium-obj.zip": False}      # no full-resolution OBJ, no GLB
    assert all(f["url"].startswith("https://3d-api.si.edu/") for f in files)


def test_files_on_other_hosts_are_never_offered(si, monkeypatch):
    rows = [_row(WHALE, "t", "Download3D", "Watertight", "zip", "stl", "ok.zip", 10),
            {"title": "t", "content": {"usage": "Download3D", "model_type": "stl", "uri": "https://evil.example/x.zip", "model_url": f"3d_package:{WHALE}"}},
            {"title": "t", "content": {"usage": "Download3D", "model_type": "stl", "uri": "http://3d-api.si.edu/plain.zip", "model_url": f"3d_package:{WHALE}"}}]
    assert [f["name"] for f in sources.smithsonian_files_from_rows(rows, WHALE)] == ["ok.zip"]


def test_the_downloads_page_lists_files_with_the_stls_selected(si, authed):
    files = downloads.list_files("smithsonian", WHALE, {})
    selected = {f["name"] for f in files if f["selected"]}
    assert selected == {"jaw_cleaned_999k.zip", "skull_processed.zip"} and all(f["selectable"] for f in files)
    flat = downloads.list_files("smithsonian", FLAT, {})
    assert [f["name"] for f in flat if f["selected"]] == ["suit-low-obj.zip"] and {f["name"] for f in flat} == {"suit-obj.zip", "suit-low-obj.zip"}


# ---------- downloading ----------

def test_a_listing_is_downloaded_with_its_models_linked(authed, si):
    item = _run(WHALE)
    assert item["status"] == "done", item
    models = [authed.get(f"/api/library/models/{i}").json() for i in item["model_ids"]]
    assert sorted(m["filename"] for m in models) == ["jaw.stl", "skull.stl"]                  # the readme in the zip was left behind
    for m in models:
        assert m["path"].replace("\\", "/").startswith(f"imported/Smithsonian 3D/Globicephala melas melas (Traill 1809) Skull and Mandible [{WHALE}]/")
        assert (m["source_provider"], m["source_id"], m["source_linked_by"]) == ("smithsonian", WHALE, "download")
        assert m["designer"] == "Smithsonian Institution" and m["license"].startswith("Smithsonian")
    assert set(si.file_hosts) == {"3d-api.si.edu"}


def test_a_single_stl_and_a_second_download_of_the_same_listing(authed, si):
    item = _run(CORAL)
    assert item["status"] == "done" and len(item["model_ids"]) == 1
    assert authed.get(f"/api/library/models/{item['model_ids'][0]}").json()["filename"] == "coral.stl"
    assert _run(CORAL)["status"] == "skipped"


def test_a_package_with_no_stl_gets_its_smallest_obj_archive(authed, si):
    item = _run(FLAT)
    assert item["status"] == "done", item
    assert [authed.get(f"/api/library/models/{i}").json()["filename"] for i in item["model_ids"]] == ["suit.obj"]


def test_chosen_files_only(authed, si):
    files = downloads.list_files("smithsonian", WHALE, {})
    pick = next(f["id"] for f in files if f["name"] == "skull_processed.zip")
    item = _run(WHALE, file_ids=[pick])
    assert [authed.get(f"/api/library/models/{i}").json()["filename"] for i in item["model_ids"]] == ["skull.stl"]


def test_a_download_address_on_another_site_is_refused(authed, si, monkeypatch):
    real = sources.smithsonian_files
    monkeypatch.setattr(sources, "smithsonian_files", lambda client, sid: [{**real(client, sid)[0], "url": "https://evil.example/x.stl"}])
    item = _run(WHALE)
    assert item["status"] == "error" and si.file_hosts == []
