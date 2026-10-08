"""The app can be installed on a phone or desktop: manifest, icons and a service worker that caches nothing."""
import io
import json

from PIL import Image
from starlette.testclient import TestClient


def _anonymous():
    from app.main import app
    return TestClient(app)


def test_the_manifest_is_public_and_points_at_real_icons():
    c = _anonymous()
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    assert r.headers["cache-control"] == "no-cache"
    manifest = json.loads(r.text)
    assert manifest["name"] == "Model Hub" and manifest["start_url"] == "/" and manifest["display"] == "standalone"
    sizes = set()
    for icon in manifest["icons"]:
        img = c.get(icon["src"])
        assert img.status_code == 200 and img.headers["content-type"] == "image/png", icon["src"]
        w, h = Image.open(io.BytesIO(img.content)).size
        assert f"{w}x{h}" == icon["sizes"]
        sizes.add(w)
    assert {192, 512} <= sizes


def test_the_service_worker_is_served_from_the_root_and_never_cached():
    r = _anonymous().get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache" and r.headers["service-worker-allowed"] == "/"
    # it only ever answers navigations that failed; it stores nothing
    assert "caches." not in r.text and "cache.put" not in r.text


def test_the_page_links_the_manifest_and_registers_the_worker():
    html = _anonymous().get("/").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in html and 'name="theme-color"' in html
    assert "apple-touch-icon" in html
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "serviceWorker.register('/sw.js')" in js
