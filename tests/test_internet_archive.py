"""The Internet Archive's copy of Thingiverse as a source, with the Archive faked."""
import io
import shutil
import zipfile

import httpx
import pytest

from app import downloads, sources

ID1, ID2 = "thingiverse-2405921", "thingiverse-3773087"
TEST_IDS = (ID1, ID2)


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


def _zip():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("files/benchy.stl", _stl(51))
        z.writestr("files/README.txt", "hi")
        z.writestr("images/photo.jpg", b"jpg")
    return out.getvalue()


SEARCH = {"response": {"numFound": 2, "docs": [
    {"identifier": ID1, "title": "2D Benchy (2405921)", "creator": "Tommy Cantwell (T-E-C)", "licenseurl": "https://creativecommons.org/licenses/by/4.0/"},
    {"identifier": "not-a-thing", "title": "Something else", "creator": "x"},
    {"identifier": ID2, "title": "Duck (3773087)", "creator": ["Boris3D Studio (boris3dstudio)"], "licenseurl": "https://creativecommons.org/licenses/by-nc-sa/4.0/"}]}}
META = {"metadata": {"title": "2D Benchy (2405921)", "creator": "Tommy Cantwell (T-E-C)", "licenseurl": "https://creativecommons.org/publicdomain/zero/1.0/",
                     "description": "<p>A <b>flat</b> boat.</p>", "subject": ["thingiverse", "3D_printing", "stl", "boat", "benchy_2d"]},
        "files": [{"name": "benchy_2405921.zip", "source": "original", "size": "1000"},
                  {"name": "thingiverse-2405921_meta.xml", "source": "original", "size": "10"},
                  {"name": "thingiverse-2405921_archive.torrent", "source": "metadata", "size": "10"},
                  {"name": "sub/dir.stl", "source": "original", "size": "5"},
                  {"name": "loose.stl", "source": "original", "size": "2000"}]}


class FakeArchive:
    def __init__(self):
        self.requests, self.status, self.hosts = [], 200, []
        self.redirect_to = None

    def __call__(self, request):
        host, path = request.url.host, request.url.path
        self.requests.append(str(request.url))
        if self.status != 200 and host == "archive.org":
            return httpx.Response(self.status)
        if host == "archive.org" and path == "/advancedsearch.php":
            return httpx.Response(200, json=SEARCH)
        if host == "archive.org" and path == "/metadata/" + ID1:
            return httpx.Response(200, json=META)
        if host == "archive.org" and path.startswith("/metadata/"):
            return httpx.Response(200, json={})
        if host == "archive.org" and path.startswith("/download/"):
            self.hosts.append(host)
            if self.redirect_to:
                return httpx.Response(302, headers={"location": self.redirect_to})
            return httpx.Response(302, headers={"location": "https://ia800809.us.archive.org/23/items/x/" + path.rsplit("/", 1)[-1]})   # the real servers keep the name
        if host.endswith(".us.archive.org"):
            self.hosts.append(host)
            return httpx.Response(200, content=_zip() if path.endswith(".zip") else _stl(77))
        if host == "evil.example":
            self.hosts.append(host)
            return httpx.Response(200, content=_stl(5))
        return httpx.Response(404)


@pytest.fixture()
def archive(monkeypatch, authed):
    fake = FakeArchive()
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
    shutil.rmtree(LIBRARY_PATH / "imported" / "Internet Archive (Thingiverse)", ignore_errors=True)


def _run(source_id):
    downloads.enqueue([{"provider": "archive", "source_id": source_id, "title": "x"}], images=False, add_tags=True)
    item = next(i for i in downloads._items if i["source_id"] == source_id and i["status"] == "queued")
    item["status"] = "downloading"
    downloads.process_item(item)
    return downloads.find_item("archive", source_id)


def test_it_is_a_keyless_download_source_not_used_for_matching():
    assert "archive" in sources.PROVIDERS and "archive" in sources.KEYLESS_PROVIDERS and "archive" in sources.DOWNLOAD_PROVIDERS
    assert "archive" not in sources.matching_providers({}) and sources.image_host_allowed("archive.org") and sources.image_host_allowed("ia800809.us.archive.org")
    assert not sources.image_host_allowed("notarchive.org")


def test_license_addresses_are_shown_as_names():
    assert sources.archive_license("https://creativecommons.org/licenses/by-nc-sa/4.0/") == "CC BY-NC-SA 4.0"
    assert sources.archive_license("https://creativecommons.org/licenses/by/3.0/") == "CC BY 3.0"
    assert sources.archive_license("http://creativecommons.org/publicdomain/zero/1.0/") == "CC0"
    assert sources.archive_license(None) == "" and sources.archive_license("https://example.com/terms") == "https://example.com/terms"


def test_a_search_only_covers_the_thingiverse_collection_and_cleans_up_the_results(archive):
    found = sources.search("benchy boat", ["archive"], limit=6, credentials={})
    assert found["errors"] == {}
    assert [r["source_id"] for r in found["results"]] == [ID1, ID2]                            # the odd identifier is dropped
    first = found["results"][0]
    assert first["title"] == "2D Benchy" and first["designer"] == "Tommy Cantwell" and first["license"] == "CC BY 4.0"
    assert first["url"] == "https://archive.org/details/" + ID1 and first["thumbnail"] == f"https://archive.org/services/img/{ID1}"
    assert found["results"][1]["designer"] == "Boris3D Studio" and found["results"][1]["license"] == "CC BY-NC-SA 4.0"
    sent = next(u for u in archive.requests if "advancedsearch" in u)
    assert "collection%3Athingiverse" in sent and "benchy+AND+boat" in sent or "benchy%20AND%20boat" in sent


def test_search_words_are_cleaned_so_they_cannot_change_the_query(archive):
    sources.search('benchy" OR collection:everything', ["archive"], limit=3, credentials={})
    sent = next(u for u in archive.requests if "advancedsearch" in u)
    assert "everything" in sent and "OR" in sent                                  # they are just words in the title search
    assert '"' not in httpx.URL(sent).params["q"] and "collection:everything" not in httpx.URL(sent).params["q"]
    with pytest.raises(sources.SourceError):
        sources.search("!!! ???", ["archive"], limit=3, credentials={}) if False else sources._search_archive(None, "!!! ???", 3)


def test_an_outage_is_reported_without_hiding_other_sites(archive):
    archive.status = 503
    found = sources.search("benchy", ["archive"], limit=6, credentials={})
    assert found["results"] == [] and "503" in found["errors"]["archive"]


def test_details_give_title_designer_license_text_and_tags(archive):
    d = sources.fetch_details("archive", ID1, {})
    assert (d["title"], d["designer"], d["license"]) == ("2D Benchy", "Tommy Cantwell", "CC0")
    assert d["description"] == "A flat boat." and d["tags"] == ["boat", "benchy 2d"] and d["images"] == [f"https://archive.org/services/img/{ID1}"]
    with pytest.raises(sources.SourceError):
        sources.fetch_details("archive", ID2, {})


def test_addresses_and_ids():
    assert sources.parse_url("https://archive.org/details/thingiverse-123") == ("archive", "thingiverse-123")
    assert sources.parse_url("https://www.archive.org/download/thingiverse-123/x.zip") == ("archive", "thingiverse-123")
    assert sources.parse_url("https://archive.org/details/some-other-item") is None
    assert sources.valid_source_id("archive", "thingiverse-9") and not sources.valid_source_id("archive", "thingiverse-9/../x") and not sources.valid_source_id("archive", "9")


def test_files_are_the_things_own_files_only(archive):
    with sources._client() as client:
        files = sources.archive_files(client, ID1)
    assert [(f["name"], f["size"]) for f in files] == [("benchy_2405921.zip", 1000), ("loose.stl", 2000)]
    assert files[0]["url"] == f"https://archive.org/download/{ID1}/benchy_2405921.zip"


def test_a_listing_is_downloaded_through_the_archives_own_servers(authed, archive):
    item = _run(ID1)
    assert item["status"] == "done", item["message"]
    models = [authed.get(f"/api/library/models/{i}").json() for i in item["model_ids"]]
    assert sorted(m["filename"] for m in models) == ["benchy.stl", "loose.stl"]
    m = models[0]
    assert m["path"].replace("\\", "/").startswith(f"imported/Internet Archive (Thingiverse)/2D Benchy [{ID1}]/")
    assert (m["source_provider"], m["source_id"], m["designer"], m["license"]) == ("archive", ID1, "Tommy Cantwell", "CC0")
    assert {t["name"] for t in m["tags"]} >= {"boat"}
    assert set(archive.hosts) <= {"archive.org", "ia800809.us.archive.org"}


def test_a_redirect_to_another_site_is_refused(authed, archive):
    archive.redirect_to = "https://evil.example/x.zip"
    item = _run(ID1)
    assert item["status"] == "error" and "evil.example" not in archive.hosts
