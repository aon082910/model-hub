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


SKETCHFAB_UID = "a4d9ebd192ea4fea9c19a8ac45e7818c"
SKETCHFAB_THUMBS = {"images": [
    {"url": "https://media.sketchfab.com/models/x/thumbnails/a/1920.jpeg", "width": 1920, "height": 1080},
    {"url": "https://media.sketchfab.com/models/x/thumbnails/b/512.jpeg", "width": 512, "height": 288},
    {"url": "https://media.sketchfab.com/models/x/thumbnails/c/256.jpeg", "width": 256, "height": 144},
]}
SKETCHFAB_MODEL = {
    "uid": SKETCHFAB_UID, "name": "3D Benchy", "description": "<p>Torture test</p>",
    "viewerUrl": f"https://sketchfab.com/3d-models/3d-benchy-{SKETCHFAB_UID}",
    "license": {"label": "CC Attribution", "fullName": "Creative Commons Attribution"},
    "user": {"displayName": "Jhon", "username": "jhon"}, "tags": [{"name": "benchy"}, {"name": "boat"}],
    "categories": [{"name": "Cars & Vehicles"}], "thumbnails": SKETCHFAB_THUMBS, "likeCount": 3, "downloadCount": 9,
}
SKETCHFAB_SEARCH = {"results": [SKETCHFAB_MODEL, {"uid": "f" * 32, "name": "Other thing", "thumbnails": SKETCHFAB_THUMBS}]}

THINGIVERSE_TOKEN = "tv-test-token"
THINGIVERSE_THING = {
    "id": 763622, "name": "#3DBenchy", "public_url": "https://www.thingiverse.com/thing:763622",
    "creator": {"name": "CreativeTools"}, "license": "Creative Commons - Attribution - No Derivatives",
    "description": "plain description", "description_html": "<p>A <b>tough</b> boat</p>", "instructions": "print it",
    "like_count": 5, "download_count": 50, "thumbnail": "https://cdn.thingiverse.com/thumb.jpg",
}
THINGIVERSE_IMAGES = [
    {"id": 1, "url": "https://cdn.thingiverse.com/img1_raw.jpg", "sizes": [
        {"type": "thumb", "size": "small", "url": "https://cdn.thingiverse.com/img1_small.jpg"},
        {"type": "display", "size": "large", "url": "https://cdn.thingiverse.com/img1_large.jpg"}]},
    {"id": 2, "url": "https://cdn.thingiverse.com/img2.jpg", "sizes": []},
]
THINGIVERSE_SEARCH = {"total": 1, "hits": [{
    "id": 763622, "name": "#3DBenchy", "public_url": "https://www.thingiverse.com/thing:763622",
    "creator": {"name": "CreativeTools"}, "thumbnail": "https://cdn.thingiverse.com/thumb.jpg"}]}


MMF_KEY = "mmf-test-key"
MMF_OBJECT = {
    "id": 11323, "url": "https://www.myminifactory.com/object/3d-print-hammered-patrick-11323", "name": "Hammered Patrick",
    "description": "<p>A <b>fun</b> figure</p>", "likes": 12, "views": 345,
    "designer": {"username": "pat", "name": "Patrick Maker"},
    "images": [
        {"id": 2, "is_primary": False, "thumbnail": {"url": "https://cdn.myminifactory.com/t2.jpg"},
         "standard": {"url": "https://cdn.myminifactory.com/s2.jpg"}, "original": {"url": "https://cdn.myminifactory.com/o2.jpg"}},
        {"id": 1, "is_primary": True, "thumbnail": {"url": "https://cdn.myminifactory.com/t1.jpg"},
         "standard": {"url": "https://cdn.myminifactory.com/s1.jpg"}, "original": {"url": "https://cdn.myminifactory.com/o1.jpg"}}],
    "categories": [{"name": "Miniatures"}], "tags": ["patrick", "figure"],
    "licenses": [{"type": "mention", "value": True}, {"type": "remix", "value": True}, {"type": "commercial-use", "value": False}],
}
MMF_SEARCH = {"total_count": 1, "items": [MMF_OBJECT, {"id": 99, "name": "Other", "images": []}]}

CULTS_USER, CULTS_KEY = "cults-nick", "cults-secret"
CULTS_URL = "https://cults3d.com/en/3d-model/art/frame-wall-hanger-f745834a-4835"
CULTS_CREATION = {
    "name": "Frame wall hanger", "url": CULTS_URL, "illustrationImageUrl": "https://images.cults3d.com/cover.jpg",
    "description": "<p>Hang <i>frames</i></p>", "license": {"name": "Creative Commons - Attribution"},
    "category": {"name": "Art"}, "tags": ["frame", "wall"], "creator": {"nick": "3DPrinterFiles"},
    "viewsCount": 10, "likesCount": 4, "downloadsCount": 7,
}


def _model_stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


def _pack_zip():
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("Benchy/hull.stl", _model_stl(31))
        z.writestr("Benchy/deck.stl", _model_stl(32))
        z.writestr("Benchy/README.txt", "print me")
        z.writestr("Benchy/preview.jpg", b"jpg")
    return out.getvalue()


PRINTABLES_FILES = {"data": {"print": {"id": "3161", "stls": [{"id": "49068", "name": "3dbenchy.stl", "fileSize": 1000}],
                                       "downloadPacks": [{"id": "7763318", "fileSize": 4000, "fileType": "MODEL_FILES"},
                                                         {"id": "7768534", "fileSize": 9, "fileType": "PRINT_FILES"}]}}}
THINGIVERSE_FILES = [
    {"id": 1, "name": "benchy.stl", "size": 100, "download_url": "https://www.thingiverse.com/download:1"},
    {"id": 2, "name": "readme.txt", "size": 5, "download_url": "https://www.thingiverse.com/download:2"},
    {"id": 3, "name": "photo.jpg", "size": 5, "download_url": "https://www.thingiverse.com/download:3"},
]


COMMONS_PAGE = {
    "pageid": 4242, "title": "File:Benchy boat.stl", "index": 1,
    "imageinfo": [{"url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Benchy_boat.stl", "size": 2048,
                   "descriptionurl": "https://commons.wikimedia.org/wiki/File:Benchy_boat.stl",
                   "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Benchy_boat.stl/1200px-x.png",
                   "extmetadata": {"Artist": {"value": "<a href='x'>CreativeTools</a>: https://www.thingiverse.com/thing:763622"},
                                   "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                   "ImageDescription": {"value": "<p>A tiny tugboat.</p>"},
                                   "Credit": {"value": "Own work"}}}],
    "thumbnail": {"source": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Benchy_boat.stl/320px-x.png"},
    "categories": [{"title": "Category:3D printing"}, {"title": "Category:Boats"}],
}
NASA_TREE = {"tree": [
    {"path": "3D Printing/Apollo 11 Landing Site/landing.stl", "type": "blob", "size": 5000},
    {"path": "3D Printing/Apollo 11 Landing Site/landing.png", "type": "blob", "size": 4000},
    {"path": "3D Printing/Apollo 11 Landing Site/big.png", "type": "blob", "size": 9000000},
    {"path": "3D Printing/Apollo 11 Landing Site/notes.pdf", "type": "blob", "size": 10},
    {"path": "3D Printing/Dawn/dawn.obj", "type": "blob", "size": 3000},
    {"path": "3D Printing/Dawn", "type": "tree"},
    {"path": "Models-only/readme.md", "type": "blob", "size": 5},
    {"path": "top.stl", "type": "blob", "size": 5},
]}


def _user_print(n, name):
    return {"id": str(n), "name": name, "slug": f"p{n}", "user": {"id": "16", "publicUsername": "Prusa Research"},
            "license": {"abbreviation": "CC0", "name": "x"}, "image": None}


class FakeSites:
    """Records calls and answers like the two sites; flip the flags to break one."""

    def __init__(self):
        self.calls = []
        self.printables_down = False
        self.makerworld_status = 200
        self.sketchfab_status = 200
        self.thingiverse_status = 200
        self.thingiverse_requests = []          # (path, authorization header, query string)
        self.mmf_status = 200
        self.mmf_requests = []                  # full request URLs (the key is in the query string there)
        self.cults_status = 200
        self.printables_files = PRINTABLES_FILES          # what the file-listing query returns
        self.printables_link = None                       # override the download link (for the safety tests)
        self.printables_file_status = 200
        self.printables_zip = None                        # override the pack contents
        self.thingiverse_files = THINGIVERSE_FILES
        self.thingiverse_redirect_to = None               # send the download somewhere else
        self.thingiverse_download_auth = []               # Authorization header seen by www.thingiverse.com
        self.cdn_download_auth = []                       # ...and by the CDN (must stay empty)
        self.file_requests = []
        self.cults_no_description = False       # the schema has no description field
        self.cults_queries = []                 # (query text, variables)
        self.cults_auth = []
        self.printables_user_prints = [_user_print(500, "Newest"), _user_print(499, "Older")]   # a designer's uploads, newest first
        self.sketchfab_user_models = [{"uid": "b" * 32, "name": "Sketch upload", "user": {"username": "jhon", "displayName": "Jhon"},
                                       "thumbnails": SKETCHFAB_THUMBS}]
        self.thingiverse_user_things = [{"id": 900, "name": "Tv upload", "creator": {"name": "CreativeTools"},
                                         "thumbnail": "https://cdn.thingiverse.com/t.jpg"}]
        self.thingiverse_likes = [{"id": 901, "name": "Liked A", "creator": {"name": "x"}}, {"id": 902, "name": "Liked B", "creator": {"name": "y"}}]
        self.thingiverse_collection = [{"id": 903, "name": "In a collection", "creator": {"name": "z"}}]
        self.printables_print = PRINTABLES_PRINT         # what the listing details query returns (tests change it)
        self.commons_status = 200
        self.nasa_status = 200
        self.file_hosts_seen = []               # hosts that served a model file (commons / nasa)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        host = request.url.host
        if host == "api.printables.com":
            if self.printables_down:
                return httpx.Response(503)
            body = json.loads(request.content)
            query_text = body["query"]
            if "morePrints" in query_text:
                return httpx.Response(200, json={"data": {"r": {"items": self.printables_user_prints}}})
            if "downloadPacks" in query_text:
                return httpx.Response(200, json=self.printables_files)
            if "getDownloadLink" in query_text:
                kind = "pack" if "fileType: pack" in query_text else "stl"
                link = self.printables_link or (
                    "https://files.printables.com/media/prints/3161/packs/7763318_x/3d-benchy-model_files.zip" if kind == "pack"
                    else "https://files.printables.com/media/prints/3161/stls/49068_x/3dbenchy.stl")
                return httpx.Response(200, json={"data": {"getDownloadLink": {"ok": True, "errors": None, "output": {"link": link, "ttl": 86400}}}})
            return httpx.Response(200, json=PRINTABLES_SEARCH if "searchPrints2" in body["query"] else self.printables_print)
        if host == "api.bambulab.com":
            if self.makerworld_status != 200:
                return httpx.Response(self.makerworld_status)
            if "/search-service/" in url:
                return httpx.Response(200, json=MAKERWORLD_SEARCH)
            return httpx.Response(200, json=MAKERWORLD_DESIGN)
        if host == "api.sketchfab.com":
            if self.sketchfab_status != 200:
                return httpx.Response(self.sketchfab_status)
            if request.url.path == "/v3/models":
                return httpx.Response(200, json={"results": self.sketchfab_user_models})
            return httpx.Response(200, json=SKETCHFAB_SEARCH if request.url.path.endswith("/search") else SKETCHFAB_MODEL)
        if host == "www.myminifactory.com" and request.url.path.startswith("/api/v2/"):
            self.mmf_requests.append(url)
            if self.mmf_status != 200:
                return httpx.Response(self.mmf_status)
            if request.url.params.get("key") != MMF_KEY:
                return httpx.Response(401, json={"error": "access_denied", "error_description": "Invalid API key."})
            if request.url.path == "/api/v2/search":
                return httpx.Response(200, json=MMF_SEARCH)
            return httpx.Response(200, json=MMF_OBJECT)
        if host == "cults3d.com" and request.url.path == "/graphql":
            body = json.loads(request.content)
            self.cults_queries.append((body["query"], body.get("variables")))
            self.cults_auth.append(request.headers.get("authorization"))
            if self.cults_status != 200:
                return httpx.Response(self.cults_status)
            import base64
            if request.headers.get("authorization") != "Basic " + base64.b64encode(f"{CULTS_USER}:{CULTS_KEY}".encode()).decode():
                return httpx.Response(401, text="HTTP Basic: Access denied.")
            if "creationsSearchBatch" in body["query"]:
                return httpx.Response(200, json={"data": {"creationsSearchBatch": {"total": 1, "results": [
                    {"name": CULTS_CREATION["name"], "url": CULTS_URL, "illustrationImageUrl": CULTS_CREATION["illustrationImageUrl"],
                     "creator": {"nick": "3DPrinterFiles"}, "license": {"name": "CC-BY"}}]}}})
            if self.cults_no_description and "description" in body["query"]:
                return httpx.Response(200, json={"errors": [{"message": "Field 'description' doesn't exist on type 'Creation'"}]})
            creation = {k: v for k, v in CULTS_CREATION.items() if not (self.cults_no_description and k == "description")}
            return httpx.Response(200, json={"data": {"creation": creation}})
        if host == "files.printables.com":
            self.file_requests.append(url)
            if self.printables_file_status != 200:
                return httpx.Response(self.printables_file_status)
            if url.endswith(".zip"):
                return httpx.Response(200, content=self.printables_zip or _pack_zip())
            return httpx.Response(200, content=_model_stl(41))
        if host == "www.thingiverse.com" and request.url.path.startswith("/download:"):
            self.thingiverse_download_auth.append(request.headers.get("authorization"))
            target = self.thingiverse_redirect_to or f"https://cdn.thingiverse.com/files/{request.url.path.split(':')[1]}/benchy.stl"
            return httpx.Response(302, headers={"location": target})
        if host == "cdn.thingiverse.com" and request.url.path.startswith("/files/"):
            self.cdn_download_auth.append(request.headers.get("authorization"))
            return httpx.Response(200, content=_model_stl(51))
        if host == "api.thingiverse.com":
            self.thingiverse_requests.append((request.url.path, request.headers.get("authorization"), request.url.query.decode()))
            if self.thingiverse_status != 200:
                return httpx.Response(self.thingiverse_status)
            if request.headers.get("authorization") != f"Bearer {THINGIVERSE_TOKEN}":
                return httpx.Response(401, json={"error": "invalid"})
            path = request.url.path
            if path == "/users/me":
                return httpx.Response(200, json={"name": "me_user"})
            if path.startswith("/users/") and path.endswith("/things"):
                return httpx.Response(200, json=self.thingiverse_user_things)
            if path.startswith("/users/") and path.endswith("/likes"):
                return httpx.Response(200, json=self.thingiverse_likes)
            if path.startswith("/collections/") and path.endswith("/things"):
                return httpx.Response(200, json=self.thingiverse_collection)
            if path.startswith("/search/"):
                return httpx.Response(200, json=THINGIVERSE_SEARCH)
            if path.endswith("/files"):
                return httpx.Response(200, json=self.thingiverse_files)
            if path.endswith("/images"):
                return httpx.Response(200, json=THINGIVERSE_IMAGES)
            if path.endswith("/tags"):
                return httpx.Response(200, json=[{"name": "Benchy"}, {"name": "calibration"}])
            return httpx.Response(200, json=THINGIVERSE_THING)
        if host == "commons.wikimedia.org" and request.url.path == "/w/api.php":
            if self.commons_status != 200:
                return httpx.Response(self.commons_status)
            params = request.url.params
            if params.get("generator") == "search":
                return httpx.Response(200, json={"query": {"pages": [COMMONS_PAGE, {"pageid": 9, "title": "File:Not a model.jpg", "index": 2, "imageinfo": [{}]}]}})
            if params.get("pageids") == "4242":
                return httpx.Response(200, json={"query": {"pages": [COMMONS_PAGE]}})
            return httpx.Response(200, json={"query": {"pages": [{"pageid": 1, "title": "File:x.stl", "missing": True}]}})
        if host == "api.github.com" and request.url.path.endswith("/git/trees/master"):
            if self.nasa_status != 200:
                return httpx.Response(self.nasa_status)
            return httpx.Response(200, json=NASA_TREE)
        if host == "3d-api.si.edu" and request.url.path.endswith("/file/search"):        # the Smithsonian: nothing matches in these tests
            return httpx.Response(200, json={"rows": [], "rowCount": 0, "message": "no results found"})
        if host in ("upload.wikimedia.org", "raw.githubusercontent.com") and not url.endswith((".png", ".jpg", ".jpeg", ".webp")):
            self.file_hosts_seen.append(host)
            return httpx.Response(200, content=_model_stl(61))
        if sources.image_host_allowed(host):
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
    assert sources.parse_url(f"https://sketchfab.com/3d-models/3d-benchy-{SKETCHFAB_UID}") == ("sketchfab", SKETCHFAB_UID)
    assert sources.parse_url(f"https://sketchfab.com/models/{SKETCHFAB_UID.upper()}") == ("sketchfab", SKETCHFAB_UID)
    assert sources.parse_url("https://www.thingiverse.com/thing:763622/files") == ("thingiverse", "763622")
    for bad in ("https://evil.com/?u=https://www.printables.com/model/1", "http://localhost/model/3",
                "https://sketchfab.com/3d-models/not-a-uid", "https://thingiverse.com/thing:abc", "", "printables.com/model/3"):
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
    assert {r["provider"] for r in found["results"]} == {"makerworld", "sketchfab", "commons"}
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
    assert {x["provider"] for x in r["results"]} == {"printables", "makerworld", "sketchfab", "commons"}   # Thingiverse needs a token
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



# ---------- Sketchfab ----------

def test_valid_source_ids():
    assert sources.valid_source_id("printables", "3161") and not sources.valid_source_id("printables", "abc")
    assert sources.valid_source_id("sketchfab", SKETCHFAB_UID) and not sources.valid_source_id("sketchfab", "3161")
    assert not sources.valid_source_id("sketchfab", SKETCHFAB_UID + "0")
    assert not sources.valid_source_id("nope", "1") and not sources.valid_source_id("printables", None)


def test_sketchfab_details_and_search(sites):
    d = sources.fetch_details("sketchfab", SKETCHFAB_UID)
    assert (d["title"], d["designer"], d["license"]) == ("3D Benchy", "Jhon", "CC Attribution")
    assert d["description"] == "Torture test" and d["tags"] == ["benchy", "boat"] and d["category"] == "Cars & Vehicles"
    assert d["images"] == ["https://media.sketchfab.com/models/x/thumbnails/a/1920.jpeg"]      # the large one only
    assert d["url"].endswith(SKETCHFAB_UID)

    found = sources.search("benchy", providers=("sketchfab",))["results"]
    assert [r["source_id"] for r in found] == [SKETCHFAB_UID, "f" * 32]
    assert found[0]["thumbnail"].endswith("/256.jpeg")                                            # closest to 320px wide
    sites.sketchfab_status = 500
    assert "sketchfab" in sources.search("benchy", providers=("sketchfab",))["errors"]


# ---------- Thingiverse ----------

def test_thingiverse_needs_a_token(sites):
    assert "thingiverse" not in sources.available_providers({})
    assert "thingiverse" in sources.available_providers({"thingiverse": {"token": THINGIVERSE_TOKEN}})
    with pytest.raises(sources.SourceError, match="access token"):
        sources.fetch_details("thingiverse", "763622")
    found = sources.search("benchy", providers=("thingiverse",))
    assert "access token" in found["errors"]["thingiverse"]
    assert sites.thingiverse_requests == []                       # nothing was sent without a token


def test_thingiverse_details_and_search(sites):
    creds = {"thingiverse": {"token": THINGIVERSE_TOKEN}}
    d = sources.fetch_details("thingiverse", "763622", creds)
    assert (d["title"], d["designer"]) == ("#3DBenchy", "CreativeTools")
    assert d["license"] == "Creative Commons - Attribution - No Derivatives"
    assert d["description"] == "A tough boat\n\nprint it"                          # html version preferred, instructions appended
    assert d["tags"] == ["Benchy", "calibration"]
    assert d["images"] == ["https://cdn.thingiverse.com/img1_large.jpg", "https://cdn.thingiverse.com/img2.jpg"]
    found = sources.search("3d benchy", providers=("thingiverse",), credentials=creds)["results"]
    assert found[0]["source_id"] == "763622" and found[0]["designer"] == "CreativeTools"
    # the token travels in a header, never in the URL
    assert all(auth == f"Bearer {THINGIVERSE_TOKEN}" for _, auth, _ in sites.thingiverse_requests)
    assert not any(THINGIVERSE_TOKEN in query or THINGIVERSE_TOKEN in path for path, _, query in sites.thingiverse_requests)
    assert any(path == "/search/3d benchy" for path, _, _ in sites.thingiverse_requests)   # httpx shows the decoded path


def test_thingiverse_errors_never_leak_the_token(sites):
    creds = {"thingiverse": {"token": "wrong-token-value"}}
    with pytest.raises(sources.SourceError, match="rejected the access token") as exc:
        sources.fetch_details("thingiverse", "763622", creds)
    assert "wrong-token-value" not in str(exc.value)
    sites.thingiverse_status = 429
    with pytest.raises(sources.SourceError, match="rate limiting"):
        sources.fetch_details("thingiverse", "763622", {"thingiverse": {"token": THINGIVERSE_TOKEN}})
    sites.thingiverse_status = 404
    with pytest.raises(sources.SourceError, match="not found"):
        sources.fetch_details("thingiverse", "763622", {"thingiverse": {"token": THINGIVERSE_TOKEN}})


def test_thingiverse_token_is_a_masked_setting_and_enables_the_provider(authed, sites):
    providers = {p["id"]: p for p in authed.get("/api/sources/providers").json()}
    assert providers["thingiverse"]["enabled"] is False and providers["thingiverse"]["needs_credentials"] is True
    assert providers["thingiverse"]["fields"] == [{"name": "token", "label": "Access token", "secret": True, "set": False}]
    assert providers["sketchfab"]["enabled"] is True and providers["sketchfab"]["needs_credentials"] is False

    assert authed.put("/api/settings", json={"thingiverse_token": THINGIVERSE_TOKEN}).status_code == 200
    try:
        assert authed.get("/api/settings").json()["thingiverse_token"] == "********"       # never echoed back
        now = {p["id"]: p for p in authed.get("/api/sources/providers").json()}
        assert now["thingiverse"]["enabled"] is True and now["thingiverse"]["fields"][0]["set"] is True
        found = authed.get("/api/sources/search", params={"q": "benchy"}).json()
        assert "thingiverse" in {r["provider"] for r in found["results"]}
        d = authed.get("/api/sources/lookup", params={"url": "https://www.thingiverse.com/thing:763622"}).json()
        assert d["title"] == "#3DBenchy"
        # saving the masked value back must not overwrite the real token
        authed.put("/api/settings", json={"thingiverse_token": "********"})
        assert "thingiverse" in {r["provider"] for r in authed.get("/api/sources/search", params={"q": "benchy"}).json()["results"]}
    finally:
        authed.put("/api/settings", json={"thingiverse_token": ""})
    assert {p["id"]: p["enabled"] for p in authed.get("/api/sources/providers").json()}["thingiverse"] is False


def test_link_a_sketchfab_listing(authed, sites):
    model = _import_model(authed, "sketchfab-link.stl")
    r = authed.post(f"/api/library/models/{model['id']}/source", json={"url": f"https://sketchfab.com/3d-models/x-{SKETCHFAB_UID}"})
    assert r.status_code == 200, r.text
    m = r.json()
    assert (m["source_provider"], m["source_id"], m["designer"], m["license"]) == ("sketchfab", SKETCHFAB_UID, "Jhon", "CC Attribution")
    assert json.loads(m["source_images"]) == ["1.jpg"]
    assert m["source_linked_by"] == "manual"
    bad = authed.post(f"/api/library/models/{model['id']}/source", json={"provider": "sketchfab", "source_id": "12345"})
    assert bad.status_code == 400


# ---------- filament a MakerWorld listing suggests ----------

def test_makerworld_filaments():
    ext = {"boms_of_filaments": [
        {"parentTitle": "PLA Basic", "title": "Gray (10103) / Refill / 1kg"},
        {"displayParentTitle": "PETG HF", "displayTitle": "Black (33102) / With spool / 1kg"},
        {"parentTitle": "PLA Basic", "title": "Gray (10103) / With spool / 1kg"},       # same colour again: listed once
        {"parentTitle": "", "title": "ignored"},
    ]}
    assert sources.makerworld_filaments(ext) == [
        {"material": "PLA", "brand": "Bambu Lab", "color": "Gray", "code": "10103", "label": "PLA Basic Gray"},
        {"material": "PETG", "brand": "Bambu Lab", "color": "Black", "code": "33102", "label": "PETG HF Black"},
    ]
    assert sources.makerworld_filaments({}) == []



# ---------- MyMiniFactory ----------

def test_parse_urls_for_myminifactory_and_cults3d():
    assert sources.parse_url("https://www.myminifactory.com/object/3d-print-hammered-patrick-11323") == ("myminifactory", "11323")
    assert sources.parse_url("https://www.myminifactory.com/fr/object/hammered-patrick-11323?x=1") == ("myminifactory", "11323")
    assert sources.parse_url(CULTS_URL) == ("cults3d", "frame-wall-hanger-f745834a-4835")
    assert sources.parse_url(CULTS_URL + "/?utm=1") == ("cults3d", "frame-wall-hanger-f745834a-4835")
    for bad in ("https://www.myminifactory.com/users/pat", "https://cults3d.com/en/users/someone", "https://cults3d.com/en/3d-model/art/"):
        assert sources.parse_url(bad) is None, bad
    assert sources.valid_source_id("cults3d", "frame-wall-hanger-f745834a") and not sources.valid_source_id("cults3d", "../etc")
    assert sources.valid_source_id("myminifactory", "11323") and not sources.valid_source_id("myminifactory", "abc")


def test_myminifactory_needs_a_key_and_sends_it(sites):
    assert "myminifactory" not in sources.available_providers({})
    with pytest.raises(sources.SourceError, match="API key"):
        sources.fetch_details("myminifactory", "11323")
    assert sites.mmf_requests == []
    creds = {"myminifactory": {"key": MMF_KEY}}
    d = sources.fetch_details("myminifactory", "11323", creds)
    assert (d["title"], d["designer"], d["category"]) == ("Hammered Patrick", "Patrick Maker", "Miniatures")
    assert d["description"] == "A fun figure" and d["tags"] == ["patrick", "figure"]
    assert d["license"] == "credit the designer, remixing allowed"                  # only the allowed ones
    assert d["images"][0] == "https://cdn.myminifactory.com/s1.jpg"                 # the primary picture first
    assert d["likes"] == 12 and d["downloads"] == 345 and d["parts"] == [] and d["filaments"] == []
    found = sources.search("patrick", providers=("myminifactory",), credentials=creds, limit=5, page=3)["results"]
    assert found[0]["source_id"] == "11323" and found[0]["thumbnail"] == "https://cdn.myminifactory.com/t1.jpg"
    assert found[1]["thumbnail"] is None
    last = sites.mmf_requests[-1]
    assert "page=3" in last and "per_page=5" in last and f"key={MMF_KEY}" in last


def test_myminifactory_errors_and_the_key_stays_out_of_the_log():
    import logging
    assert logging.getLogger("httpx").level >= logging.WARNING      # httpx would log the key-bearing URL at INFO


@pytest.mark.parametrize("status,fragment", [(404, "not found"), (429, "rate limiting"), (500, r"error \(500\)")])
def test_myminifactory_status_codes(sites, status, fragment):
    sites.mmf_status = status
    with pytest.raises(sources.SourceError, match=fragment):
        sources.fetch_details("myminifactory", "1", {"myminifactory": {"key": MMF_KEY}})


def test_myminifactory_wrong_key_message_never_contains_the_key(sites):
    with pytest.raises(sources.SourceError, match="rejected the API key") as exc:
        sources.fetch_details("myminifactory", "1", {"myminifactory": {"key": "the-wrong-key"}})
    assert "the-wrong-key" not in str(exc.value)


# ---------- Cults3D ----------

def test_cults3d_needs_both_credentials(sites):
    for partial in ({}, {"cults3d": {"username": CULTS_USER, "key": ""}}, {"cults3d": {"username": "", "key": CULTS_KEY}}):
        assert "cults3d" not in sources.available_providers(partial)
        with pytest.raises(sources.SourceError, match="nickname and API key"):
            sources.fetch_details("cults3d", "frame-wall", partial)
    assert sites.cults_queries == []


def test_cults3d_search_and_details(sites):
    creds = {"cults3d": {"username": CULTS_USER, "key": CULTS_KEY}}
    found = sources.search("frame", providers=("cults3d",), credentials=creds)["results"]
    assert found == [{
        "provider": "cults3d", "source_id": "frame-wall-hanger-f745834a-4835", "url": CULTS_URL, "title": "Frame wall hanger",
        "designer": "3DPrinterFiles", "license": "CC-BY", "thumbnail": "https://images.cults3d.com/cover.jpg"}]
    first_query, first_vars = sites.cults_queries[-1]
    assert "offset" not in first_query and first_vars == {"q": "frame", "limit": 6}
    sources.search("frame", providers=("cults3d",), credentials=creds, page=3, limit=4)
    paged_query, paged_vars = sites.cults_queries[-1]
    assert "$offset: Int" in paged_query and paged_vars == {"q": "frame", "limit": 4, "offset": 8}

    d = sources.fetch_details("cults3d", "frame-wall-hanger-f745834a-4835", creds)
    assert (d["title"], d["designer"], d["license"], d["category"]) == ("Frame wall hanger", "3DPrinterFiles", "Creative Commons - Attribution", "Art")
    assert d["description"] == "Hang frames" and d["tags"] == ["frame", "wall"] and d["images"] == ["https://images.cults3d.com/cover.jpg"]
    assert (d["likes"], d["downloads"]) == (4, 7)
    assert sites.cults_auth[-1].startswith("Basic ")                                      # HTTP Basic: nickname + key
    assert CULTS_KEY not in sites.cults_auth[-1]                                         # (base64, not the plain key)


def test_cults3d_details_fall_back_when_the_schema_has_no_description(sites):
    sites.cults_no_description = True
    d = sources.fetch_details("cults3d", "frame-wall-hanger-f745834a-4835", {"cults3d": {"username": CULTS_USER, "key": CULTS_KEY}})
    assert d["title"] == "Frame wall hanger" and d["description"] == ""
    assert len(sites.cults_queries) == 2 and "description" not in sites.cults_queries[-1][0]


@pytest.mark.parametrize("status,fragment", [(429, "rate limiting"), (500, r"error \(500\)")])
def test_cults3d_status_codes(sites, status, fragment):
    sites.cults_status = status
    with pytest.raises(sources.SourceError, match=fragment):
        sources.fetch_details("cults3d", "frame-wall", {"cults3d": {"username": CULTS_USER, "key": CULTS_KEY}})


def test_cults3d_wrong_credentials(sites):
    found = sources.search("frame", providers=("cults3d",), credentials={"cults3d": {"username": CULTS_USER, "key": "wrong"}})
    assert "rejected the nickname / API key" in found["errors"]["cults3d"]


# ---------- credentials in Settings, tested from the GUI ----------

def test_all_site_credentials_are_settings_and_secrets_are_masked(authed, sites):
    authed.put("/api/settings", json={
        "myminifactory_key": MMF_KEY, "cults3d_username": CULTS_USER, "cults3d_key": CULTS_KEY})
    try:
        masked = authed.get("/api/settings").json()
        assert masked["myminifactory_key"] == "********" and masked["cults3d_key"] == "********"
        assert masked["cults3d_username"] == CULTS_USER                   # a nickname is not a secret
        info = {p["id"]: p for p in authed.get("/api/sources/providers").json()}
        assert info["myminifactory"]["enabled"] is True and info["cults3d"]["enabled"] is True
        assert [f["set"] for f in info["cults3d"]["fields"]] == [True, True]
        assert info["printables"]["can_download"] and info["thingiverse"]["can_download"]
        for blocked in ("makerworld", "sketchfab", "myminifactory", "cults3d"):
            assert info[blocked]["can_download"] is False and info[blocked]["download_note"], blocked

        ok = authed.post("/api/sources/test/myminifactory").json()
        assert ok == {"ok": True, "message": "MyMiniFactory accepted the credentials."}
        ok = authed.post("/api/sources/test/cults3d").json()
        assert ok["ok"] is True

        authed.put("/api/settings", json={"myminifactory_key": "not-the-key"})
        bad = authed.post("/api/sources/test/myminifactory").json()
        assert bad["ok"] is False and "rejected the API key" in bad["message"] and "not-the-key" not in bad["message"]
    finally:
        authed.put("/api/settings", json={"myminifactory_key": "", "cults3d_username": "", "cults3d_key": ""})
    assert authed.post("/api/sources/test/myminifactory").status_code == 400          # nothing saved any more
    assert authed.post("/api/sources/test/printables").status_code == 404             # no credentials to test


# ---------- Wikimedia Commons and NASA 3D Resources ----------

@pytest.fixture(autouse=True)
def _fresh_nasa_cache():
    sources.reset_nasa_cache()
    yield
    sources.reset_nasa_cache()


def test_commons_search_details_and_files(sites):
    found = sources.search("benchy", ["commons"], limit=5, credentials={})["results"]
    assert [r["source_id"] for r in found] == ["4242"]                     # the .jpg page is not a model
    assert found[0]["title"] == "Benchy boat" and found[0]["license"] == "CC BY-SA 4.0"
    assert found[0]["designer"] == "CreativeTools"                         # the link text is dropped
    d = sources.fetch_details("commons", "4242", {})
    assert d["description"] == "A tiny tugboat.\n\nSource: Own work" and d["tags"] == ["3D printing", "Boats"]
    with sources._client() as client:
        files = sources.commons_files(client, "4242")
    assert files == [{"id": "4242", "name": "Benchy boat.stl", "size": 2048,
                      "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Benchy_boat.stl"}]


def test_commons_missing_page_and_outage(sites):
    with pytest.raises(sources.SourceError):
        sources.fetch_details("commons", "777", {})
    sites.commons_status = 503
    with pytest.raises(sources.SourceError):
        sources.fetch_details("commons", "4242", {})


def test_nasa_listings_are_the_folders_holding_models(sites):
    found = sources.search("apollo", ["nasa3d"], limit=10, credentials={})["results"]
    assert [r["title"] for r in found] == ["Apollo 11 Landing Site"]
    assert found[0]["thumbnail"].endswith("landing.png")                   # the 9 MB picture is skipped
    assert found[0]["designer"] == "NASA" and found[0]["license"] == "NASA public domain"
    d = sources.fetch_details("nasa3d", found[0]["source_id"], {})
    assert d["title"] == "Apollo 11 Landing Site" and "landing.stl" in d["description"]
    with sources._client() as client:
        files = sources.nasa_files(client, found[0]["source_id"])
    assert [f["name"] for f in files] == ["landing.stl"] and files[0]["url"].startswith("https://raw.githubusercontent.com/")
    assert sources.valid_source_id("nasa3d", found[0]["source_id"]) and not sources.valid_source_id("nasa3d", "../x")


def test_nasa_url_parsing_and_tree_is_cached(sites):
    folder = "3D Printing/Dawn"
    expected = ("nasa3d", sources.nasa_listing_id(folder))
    assert sources.parse_url("https://github.com/nasa/NASA-3D-Resources/tree/master/3D%20Printing/Dawn") == expected
    assert sources.parse_url("https://github.com/nasa/NASA-3D-Resources/blob/master/3D%20Printing/Dawn/dawn.obj") == expected
    sources.search("dawn", ["nasa3d"], limit=5, credentials={})
    sources.search("apollo", ["nasa3d"], limit=5, credentials={})
    assert sum("git/trees" in c for c in sites.calls) == 1                  # one tree request serves both searches


def test_nasa_failures_become_messages(sites):
    sites.nasa_status = 403
    with pytest.raises(sources.SourceError, match="rate limiting"):
        sources.fetch_details("nasa3d", sources.nasa_listing_id("3D Printing/Dawn"), {})
    sources.reset_nasa_cache()
    sites.nasa_status = 500
    with pytest.raises(sources.SourceError):
        sources.fetch_details("nasa3d", sources.nasa_listing_id("3D Printing/Dawn"), {})
