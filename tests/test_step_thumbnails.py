"""Pictures for STEP models, rendered by the browser and uploaded."""
import io
import uuid

import pytest
from PIL import Image
from starlette.testclient import TestClient

PW = "a long enough password"


def _png(size=(320, 240), color=(30, 120, 200), fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


@pytest.fixture()
def step_model(authed):
    n = uuid.uuid4().hex
    body = f"ISO-10303-21;\nHEADER;\nFILE_NAME('{n}.step');\nENDSEC;\nDATA;\n#1=CARTESIAN_POINT('',(0.,0.,0.));\nENDSEC;\nEND-ISO-10303-21;\n"
    r = authed.post("/api/library/import", files={"file": (f"{n[:8]}.step", body.encode(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    m = r.json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


def _upload(c, model_id, data, name="thumb.png"):
    return c.post(f"/api/library/models/{model_id}/thumbnail", files={"file": (name, data, "image/png")})


def test_a_picture_is_saved_shrunk_and_served(authed, step_model):
    assert step_model["thumbnail_path"] is None
    r = _upload(authed, step_model["id"], _png((1200, 900)))
    assert r.status_code == 200, r.text
    name = r.json()["thumbnail_path"]
    assert name.startswith("client-") and name.endswith(".png")
    assert authed.get(f"/api/library/models/{step_model['id']}").json()["thumbnail_path"] == name
    served = authed.get(f"/api/library/thumbnails/{name}")
    assert served.status_code == 200 and max(Image.open(io.BytesIO(served.content)).size) <= 480


def test_jpeg_and_a_replacement_are_accepted(authed, step_model):
    assert _upload(authed, step_model["id"], _png(fmt="JPEG"), "a.jpg").status_code == 200
    assert _upload(authed, step_model["id"], _png(color=(200, 20, 20))).status_code == 200


@pytest.mark.parametrize("data", [b"", b"not a picture", b"GIF89a" + b"\x00" * 20])
def test_things_that_are_not_pictures_are_refused(authed, step_model, data):
    r = _upload(authed, step_model["id"], data)
    assert r.status_code == 400 and "usable picture" in r.json()["detail"]
    assert authed.get(f"/api/library/models/{step_model['id']}").json()["thumbnail_path"] is None


def test_a_gif_or_oversized_picture_is_refused(authed, step_model, monkeypatch):
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    assert _upload(authed, step_model["id"], gif.getvalue(), "a.gif").status_code == 400
    from app.routers import library
    monkeypatch.setattr(library, "MAX_THUMBNAIL_UPLOAD", 100)
    assert _upload(authed, step_model["id"], _png((300, 300))).status_code == 413


def test_models_the_server_renders_itself_are_refused(authed):
    stl = (b"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 61 0 0\n  vertex 0 61 0\n endloop\nendfacet\nendsolid t\n")
    m = authed.post("/api/library/import", files={"file": (f"mesh_{uuid.uuid4().hex[:5]}.stl", stl + uuid.uuid4().hex.encode(), "application/octet-stream")}).json()
    try:
        r = _upload(authed, m["id"], _png())
        assert r.status_code == 400 and "server makes" in r.json()["detail"]
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_unknown_models_and_viewers(authed, step_model):
    assert _upload(authed, 987654, _png()).status_code == 404
    authed.post("/api/users", json={"username": "thumbviewer", "password": PW, "role": "viewer"})
    from app.main import app
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "thumbviewer", "password": PW})
        assert _upload(viewer, step_model["id"], _png()).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert _upload(TestClient(app), step_model["id"], _png()).status_code == 401
