"""The page must not let a browser keep running a previous release's JS/CSS."""
import re


def test_index_versions_app_assets_and_is_not_cached(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    for name in ("style.css", "library-controls.js", "thumbnail-controls.js", "app.js"):
        assert re.search(rf'/assets/{re.escape(name)}\?v=[0-9a-f]{{10}}"', r.text), name
    # one version token shared by all of them
    assert len(set(re.findall(r"\?v=([0-9a-f]{10})", r.text))) == 1


def test_version_changes_when_an_asset_changes(monkeypatch):
    from app import main

    before = main._asset_version()
    original = main.STATIC_DIR
    import shutil
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "static"
        shutil.copytree(original, copy, ignore=shutil.ignore_patterns("vendor"))
        monkeypatch.setattr(main, "STATIC_DIR", copy)
        assert main._asset_version() == before
        (copy / "app.js").write_text((copy / "app.js").read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
        assert main._asset_version() != before


def test_assets_must_revalidate(client):
    for path in ("/assets/app.js", "/assets/style.css", "/assets/vendor/three.module.min.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-cache", path
        assert r.headers.get("etag"), path
        # revalidation works: same ETag -> 304 with no body
        again = client.get(path, headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304, path
