"""Read-only gallery links: a collection, or the whole library, shown to someone without a login."""
import uuid

import pytest
from starlette.testclient import TestClient

PW = "a long enough password"


def _stl(n, z=0):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 {z}\n  vertex {n} 0 {z}\n  vertex 0 {n} {z}\n endloop\nendfacet\nendsolid t\n").encode()


def _import(c, name, content):
    r = c.post("/api/library/import", files={"file": (name, content, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def _anonymous():
    from app.main import app
    return TestClient(app)


def _share(c, **fields):
    r = c.post("/api/shares", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def gallery(authed):
    tag = uuid.uuid4().hex[:6]
    inside = _import(authed, f"{tag}_inside.stl", _stl(40 + int(tag[:2], 16) % 20))
    other = _import(authed, f"{tag}_outside.stl", _stl(70 + int(tag[:2], 16) % 20, z=2))
    collection = authed.post("/api/collections", json={"name": f"Gifts {tag}"}).json()
    authed.post(f"/api/collections/{collection['id']}/models/{inside['id']}")
    authed.patch(f"/api/library/models/{inside['id']}", json={"designer": "Dana", "notes": "PRIVATE NOTE TEXT"})
    authed.patch(f"/api/library/models/{other['id']}", json={"notes": "OTHER PRIVATE NOTE"})
    yield tag, collection, inside, other
    authed.delete(f"/api/collections/{collection['id']}")
    for m in (inside, other):
        authed.delete(f"/api/library/models/{m['id']}")
    for l in authed.get("/api/shares").json():
        authed.delete(f"/api/shares/{l['id']}")


def test_a_collection_link_shows_only_that_collection(authed, gallery):
    tag, collection, inside, other = gallery
    link = _share(authed, kind="collection", target_id=collection["id"])
    assert link["target_name"] == collection["name"]
    r = _anonymous().get(link["path"])
    assert r.status_code == 200 and "default-src 'none'" in r.headers["content-security-policy"]
    assert inside["filename"] in r.text and other["filename"] not in r.text and collection["name"] in r.text
    assert "PRIVATE NOTE TEXT" not in r.text and "1 model" in r.text
    assert f"{link['path']}/model/{inside['id']}" in r.text                                           # each model links to its own page


def test_a_model_page_inside_a_gallery_shows_only_what_a_single_model_link_would(authed, gallery):
    tag, collection, inside, other = gallery
    link = _share(authed, kind="collection", target_id=collection["id"])
    anon = _anonymous()
    page = anon.get(f"{link['path']}/model/{inside['id']}")
    assert page.status_code == 200 and "Dana" in page.text and "PRIVATE NOTE TEXT" not in page.text
    assert f'href="{link["path"]}"' in page.text and "Download file" not in page.text
    assert anon.get(f"{link['path']}/model/{other['id']}").status_code == 404                        # not in the collection
    assert anon.get(f"{link['path']}/model/987654").status_code == 404


def test_pictures_and_files_only_for_models_the_link_shows(authed, gallery):
    tag, collection, inside, other = gallery
    anon = _anonymous()
    no_downloads = _share(authed, kind="collection", target_id=collection["id"])
    assert anon.get(f"{no_downloads['path']}/file/{inside['id']}").status_code == 404
    with_downloads = _share(authed, kind="collection", target_id=collection["id"], allow_downloads=True)
    assert anon.get(f"{with_downloads['path']}/file/{inside['id']}").status_code == 200
    assert anon.get(f"{with_downloads['path']}/file/{other['id']}").status_code == 404
    assert anon.get(f"{with_downloads['path']}/thumb/{other['id']}").status_code == 404
    assert "Download file" in anon.get(f"{with_downloads['path']}/model/{inside['id']}").text


def test_a_collection_link_follows_the_collection_as_it_changes(authed, gallery):
    tag, collection, inside, other = gallery
    link = _share(authed, kind="collection", target_id=collection["id"])
    authed.post(f"/api/collections/{collection['id']}/models/{other['id']}")
    assert other["filename"] in _anonymous().get(link["path"]).text
    authed.delete(f"/api/collections/{collection['id']}/models/{inside['id']}")
    page = _anonymous().get(link["path"]).text
    assert inside["filename"] not in page and other["filename"] in page


def test_deleting_the_collection_ends_its_links_for_good(authed, gallery):
    tag, collection, inside, other = gallery
    link = _share(authed, kind="collection", target_id=collection["id"])
    authed.delete(f"/api/collections/{collection['id']}")
    assert _anonymous().get(link["path"]).status_code == 404
    assert not [l for l in authed.get("/api/shares").json() if l["id"] == link["id"]]
    again = authed.post("/api/collections", json={"name": "reused id"}).json()          # SQLite may hand the same id to a new collection
    try:
        assert not [l for l in authed.get("/api/shares").json() if l["kind"] == "collection" and l["target_id"] == again["id"]]
    finally:
        authed.delete(f"/api/collections/{again['id']}")


def test_a_whole_library_link_pages_through_everything_and_hides_private_things(authed, gallery, monkeypatch):
    tag, collection, inside, other = gallery
    from app.routers import shares
    monkeypatch.setattr(shares, "PAGE_SIZE", 1)
    link = _share(authed, kind="library", target_id=0)
    assert link["target_name"] == "the whole library"
    anon = _anonymous()
    first = anon.get(link["path"])
    assert first.status_code == 200 and "Next" in first.text and "Page 1 of" in first.text
    total = authed.get("/api/library/models", params={"limit": 1}).headers["X-Total-Count"]
    assert f"{total} model" in first.text
    seen = set()
    page = 1
    while True:
        text = anon.get(link["path"], params={"page": page}).text
        seen.update(name for name in (inside["filename"], other["filename"]) if name in text)
        assert "PRIVATE NOTE TEXT" not in text and "OTHER PRIVATE NOTE" not in text
        if "Next" not in text:
            break
        page += 1
    assert seen == {inside["filename"], other["filename"]}
    assert anon.get(link["path"], params={"page": 99999}).status_code == 200                  # past the end: an empty page, not an error
    assert anon.get(link["path"], params={"page": "x"}).status_code == 422
    assert anon.get(link["path"], params={"page": -5}).status_code == 200


def test_a_library_link_serves_any_models_picture_and_page(authed, gallery):
    tag, collection, inside, other = gallery
    link = _share(authed, kind="library", target_id=0)
    anon = _anonymous()
    assert anon.get(f"{link['path']}/model/{other['id']}").status_code == 200
    assert anon.get(f"{link['path']}/file/{other['id']}").status_code == 404                   # no downloads unless asked for


def test_only_the_administrator_can_share_the_whole_library(authed, gallery):
    tag, collection, inside, other = gallery
    authed.post("/api/users", json={"username": "galmember", "password": PW, "role": "member"})
    try:
        member = _anonymous()
        member.post("/api/auth/login", json={"username": "galmember", "password": PW})
        refused = member.post("/api/shares", json={"kind": "library", "target_id": 0})
        assert refused.status_code == 403 and "administrator" in refused.json()["detail"]
        assert member.post("/api/shares", json={"kind": "collection", "target_id": collection["id"]}).status_code == 200      # a collection is fine
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


def test_unknown_collections_and_bad_ids_are_refused(authed, gallery):
    assert authed.post("/api/shares", json={"kind": "collection", "target_id": 987654}).status_code == 404
    assert authed.post("/api/shares", json={"kind": "collection", "target_id": "1"}).status_code == 400
    assert authed.post("/api/shares", json={"kind": "everything", "target_id": 1}).status_code == 400


def test_markup_in_a_collection_or_model_name_cannot_run(authed, gallery):
    tag, collection, inside, other = gallery
    evil = authed.post("/api/collections", json={"name": "<script>alert(1)</script>"}).json()
    try:
        authed.post(f"/api/collections/{evil['id']}/models/{inside['id']}")
        link = _share(authed, kind="collection", target_id=evil["id"])
        page = _anonymous().get(link["path"]).text
        assert "<script>" not in page and "&lt;script&gt;" in page
    finally:
        authed.delete(f"/api/collections/{evil['id']}")


def test_expired_and_revoked_gallery_links_look_like_any_missing_link(authed, gallery):
    tag, collection, inside, other = gallery
    link = _share(authed, kind="collection", target_id=collection["id"], expires_days=1)
    assert _anonymous().get(link["path"]).status_code == 200
    authed.delete(f"/api/shares/{link['id']}")
    anon = _anonymous()
    assert anon.get(link["path"]).status_code == 404
    assert anon.get(f"{link['path']}/model/{inside['id']}").status_code == 404
    assert anon.get(f"{link['path']}/thumb/{inside['id']}").status_code == 404
