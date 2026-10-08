"""Bulk unlinking, and the browser extension (bundled download + manifest consistency)."""
import io
import json
import re
import zipfile
from pathlib import Path

import httpx
import pytest

from app import sources
from test_sources import FakeSites, SKETCHFAB_UID

EXTENSION = Path(__file__).resolve().parent.parent / "browser-extension"


@pytest.fixture()
def sites(monkeypatch, authed):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    return fake


def _model(c, name, n):
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
           " endloop\nendfacet\nendsolid t\n").encode()
    r = c.post("/api/library/import", files={"file": (name, stl, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _link(c, model_id, provider="printables", source_id="3161"):
    r = c.post(f"/api/library/models/{model_id}/source", json={"provider": provider, "source_id": source_id})
    assert r.status_code == 200, r.text


def _set_linked_by(model_id, value):
    from sqlmodel import Session
    from app.db import engine
    from app.models import Model3D
    with Session(engine) as session:
        model = session.get(Model3D, model_id)
        model.source_linked_by = value
        session.add(model)
        session.commit()


def _linked_ids(c, **params):
    out, offset = set(), 0
    while True:
        page = c.get("/api/source-match/linked", params={"limit": 100, "offset": offset, **params}).json()
        out |= {i["id"] for i in page["items"]}
        offset += 100
        if offset >= page["total"]:
            return out


# ---------- bulk unlink ----------

def test_linked_models_can_be_listed_and_filtered(authed, sites):
    a, b, c = _model(authed, "unl_a.stl", 71), _model(authed, "unl_b.stl", 72), _model(authed, "unl_c.stl", 73)
    _link(authed, a)
    _link(authed, b, "sketchfab", SKETCHFAB_UID)
    _set_linked_by(a, "auto")
    page = authed.get("/api/source-match/linked", params={"q": "unl_"}).json()
    assert page["total"] == 2 and {i["id"] for i in page["items"]} == {a, b}      # c is not linked
    entry = next(i for i in page["items"] if i["id"] == a)
    assert entry["source_provider"] == "printables" and entry["source_linked_by"] == "auto" and entry["source_title"] == "3D BENCHY"
    assert {i["id"] for i in authed.get("/api/source-match/linked", params={"q": "unl_", "provider": "sketchfab"}).json()["items"]} == {b}
    assert {i["id"] for i in authed.get("/api/source-match/linked", params={"q": "unl_", "linked_by": "auto"}).json()["items"]} == {a}
    assert authed.get("/api/source-match/linked", params={"linked_by": "nonsense"}).status_code == 400
    small = authed.get("/api/source-match/linked", params={"q": "unl_", "limit": 1}).json()
    assert small["total"] == 2 and len(small["items"]) == 1
    assert c not in _linked_ids(authed)


def test_unlink_chosen_models(authed, sites):
    a, b, keep = _model(authed, "unl_sel_a.stl", 74), _model(authed, "unl_sel_b.stl", 75), _model(authed, "unl_sel_keep.stl", 76)
    for m in (a, b, keep):
        _link(authed, m)
    assert authed.get(f"/api/library/models/{a}/source/images/1.jpg").status_code == 200
    r = authed.post("/api/source-match/unlink", json={"model_ids": [a, b, 999999]})
    assert r.json() == {"unlinked": 2}                                            # the unknown id is ignored
    for m in (a, b):
        full = authed.get(f"/api/library/models/{m}").json()
        assert full["source_provider"] is None and full["source_images"] is None and full["source_linked_by"] is None
    assert authed.get(f"/api/library/models/{a}/source/images/1.jpg").status_code == 404      # pictures deleted too
    assert authed.get(f"/api/library/models/{keep}").json()["source_provider"] == "printables"
    assert authed.post("/api/source-match/unlink", json={"model_ids": [a]}).json() == {"unlinked": 0}      # already unlinked


def test_unlink_everything_matching_a_filter(authed, sites):
    auto1, auto2, manual = _model(authed, "unl_f_a.stl", 77), _model(authed, "unl_f_b.stl", 78), _model(authed, "unl_f_c.stl", 79)
    for m in (auto1, auto2, manual):
        _link(authed, m)
    _set_linked_by(auto1, "auto")
    _set_linked_by(auto2, "auto")
    done = authed.post("/api/source-match/unlink", json={"linked_by": "auto", "q": "unl_f_"}).json()
    assert done == {"unlinked": 2}
    assert authed.get(f"/api/library/models/{manual}").json()["source_provider"] == "printables"
    assert authed.get(f"/api/library/models/{auto1}").json()["source_provider"] is None


def test_unlink_needs_ids_a_filter_or_an_explicit_all(authed, sites):
    for bad in ({}, {"q": "  "}, {"all": False}, {"model_ids": "x"}, {"model_ids": ["1"]}, {"linked_by": "bogus"}):
        assert authed.post("/api/source-match/unlink", json=bad).status_code == 400, bad
    assert authed.post("/api/source-match/unlink", json={"model_ids": list(range(5001))}).status_code == 400
    mid = _model(authed, "unl_all.stl", 80)
    _link(authed, mid)
    assert authed.post("/api/source-match/unlink", json={"all": True}).json()["unlinked"] >= 1
    assert authed.get(f"/api/library/models/{mid}").json()["source_provider"] is None


def test_unlinked_models_return_to_the_unchecked_pool(authed, sites):
    mid = _model(authed, "unl_pool.stl", 81)
    _link(authed, mid)
    before = authed.get("/api/source-match/status").json()["summary"]["linked"]
    authed.post("/api/source-match/unlink", json={"model_ids": [mid]})
    assert authed.get("/api/source-match/status").json()["summary"]["linked"] == before - 1


# ---------- browser extension ----------

def test_the_extension_can_be_downloaded_from_the_app(authed):
    r = authed.get("/api/settings/extension.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "model-hub-extension.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = set(z.namelist())
        assert {"manifest.json", "content.js", "background.js", "popup.html", "popup.js", "content.css"} <= names
        assert json.loads(z.read("manifest.json"))["manifest_version"] == 3
        assert not any("key" in n.lower() and n.endswith(".txt") for n in names)         # no secrets travel in the bundle


def test_extension_download_needs_a_login(client):
    client.post("/api/auth/logout")
    assert client.get("/api/settings/extension.zip").status_code == 401
    from conftest import ensure_authenticated
    ensure_authenticated(client)


def test_extension_manifest_and_content_script_agree():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    content = (EXTENSION / "content.js").read_text(encoding="utf-8")
    matches = manifest["content_scripts"][0]["matches"]
    hosts = {"printables.com", "makerworld.com", "thingiverse.com", "myminifactory.com", "cults3d.com", "sketchfab.com"}
    for host in hosts:
        assert f"*://*.{host}/*" in matches, host                     # the script runs there
        assert f"*://*.{host}/*" in manifest["host_permissions"], host  # and may fetch from there
        assert host.replace(".", "\\.") in content, host              # and knows what its pages look like
    # downloads for MakerWorld come from other hosts than its pages
    assert "*://*.bblmw.com/*" in manifest["host_permissions"]
    # works in Chrome (service worker) and Firefox (event page + an add-on id)
    assert manifest["background"]["service_worker"] == "background.js" and manifest["background"]["scripts"] == ["background.js"]
    assert manifest["browser_specific_settings"]["gecko"]["id"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_extension_downloads_with_your_login_and_checks_what_it_got():
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert "credentials: 'include'" in background                      # your logged-in session, for sites that need it
    assert "text/html" in background                                  # a login page is not a model
    assert "ALLOWED_EXTENSIONS" in background and "X-Model-Hub-Api-Key" in background
    content = (EXTENSION / "content.js").read_text(encoding="utf-8")
    assert "innerHTML" not in content                                 # page-supplied text is never parsed as HTML
