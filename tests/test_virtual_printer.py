"""A virtual printer for slicers: the OctoPrint and Moonraker upload interfaces, the inbox, and who may use them."""
import io
import uuid
import zipfile

import pytest
from starlette.testclient import TestClient

from app.models import SlicerUpload

PW = "a long enough password"
GCODE = b"G28\n; layer_height = 0.2\n; estimated printing time (normal mode) = 40m\n; filament used [g] = 12.5\n"


def _stl():
    n = 30 + uuid.uuid4().int % 300
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 4}\n endloop\nendfacet\nendsolid t\n").encode() + uuid.uuid4().hex.encode()


def _u():
    return "z" + uuid.uuid4().hex[:6]


def _model(c, name):
    r = c.post("/api/library/import", files={"file": (name, _stl(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def _3mf():
    cube = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    n = 5 + uuid.uuid4().int % 40
    verts = "".join(f'<vertex x="{x * n}" y="{y * n}" z="{z * n}"/>' for x, y, z in cube)
    faces = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    model = ('<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources><object id="1" type="model"><mesh>'
             f'<vertices>{verts}</vertices><triangles>{faces}</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/slice_info.config", '<config><plate><metadata key="prediction" value="1800"/><metadata key="weight" value="9.5"/><filament id="1" type="PLA" used_g="9.5"/></plate></config>')
    return out.getvalue()


@pytest.fixture()
def key(authed):
    authed.post("/api/settings/virtual-printer", json={"enabled": True})
    token = authed.post("/api/tokens", json={"name": "slicer", "scope": "write"}).json()
    yield token["token"]
    authed.post("/api/settings/virtual-printer", json={"enabled": False})
    for row in authed.get("/api/slicer-inbox").json()["files"]:
        authed.delete(f"/api/slicer-inbox/{row['id']}")
    for t in authed.get("/api/tokens").json()["tokens"]:
        authed.delete(f"/api/tokens/{t['id']}")
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")


def _client():
    from app.main import app
    return TestClient(app)


def _send(key, name, data=GCODE, path="/api/files/local", **form):
    return _client().post(path, headers={"X-Api-Key": key}, files={"file": (name, data, "application/octet-stream")}, data={"print": "false", **form})


# ---------------------------------------------------------------- who may use it
def test_it_is_off_until_switched_on_and_then_needs_a_write_token(authed):
    c = _client()
    assert authed.get("/api/settings/virtual-printer").json()["enabled"] is False
    token = authed.post("/api/tokens", json={"name": "t", "scope": "write"}).json()["token"]
    try:
        assert c.get("/api/version", headers={"X-Api-Key": token}).status_code == 404                                    # switched off
        assert authed.post("/api/settings/virtual-printer", json={"enabled": "yes"}).status_code == 400
        assert authed.post("/api/settings/virtual-printer", json={"enabled": True}).json() == {"enabled": True}
        assert c.get("/api/version").status_code == 401 and c.get("/api/version", headers={"X-Api-Key": "mh_wrong"}).status_code == 401
        v = c.get("/api/version", headers={"X-Api-Key": token})
        assert v.status_code == 200 and "OctoPrint" in v.json()["text"] and v.json()["api"] and v.json()["server"]
        read = authed.post("/api/tokens", json={"name": "r", "scope": "read"}).json()["token"]
        imp = authed.post("/api/tokens", json={"name": "i", "scope": "import"}).json()["token"]
        assert c.get("/api/version", headers={"X-Api-Key": read}).status_code == 200                                      # may look
        assert _send(read, "x.gcode").status_code == 403                                                                  # not write
        assert c.get("/api/version", headers={"X-Api-Key": imp}).status_code == 401                                       # an import-only key is not enough
        assert c.get("/api/library/models", headers={"X-Api-Key": token}).status_code == 401                              # the key opens only the printer's paths
        assert c.get("/api/settings/virtual-printer", headers={"X-Api-Key": token}).status_code == 401
    finally:
        authed.post("/api/settings/virtual-printer", json={"enabled": False})
        for t in authed.get("/api/tokens").json()["tokens"]:
            authed.delete(f"/api/tokens/{t['id']}")


def test_only_the_administrator_sets_it_up_and_is_told_how(authed, key):
    from app.main import app
    info = authed.get("/api/settings/virtual-printer").json()
    assert info["enabled"] is True and info["address"].startswith("http") and any("OctoPrint" in line for line in info["how"])
    authed.post("/api/users", json={"username": "vpmember", "password": PW, "role": "member"})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "vpmember", "password": PW})
        assert member.get("/api/settings/virtual-printer").status_code == 403 and member.post("/api/settings/virtual-printer", json={"enabled": False}).status_code == 403
        assert member.get("/api/slicer-inbox").status_code == 200                                                           # members see and file what arrived
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


# ---------------------------------------------------------------- OctoPrint uploads
def test_a_sliced_file_is_kept_with_the_model_it_is_named_after(authed, key):
    u = _u()
    m = _model(authed, f"benchy{u}.stl")
    try:
        r = _send(key, f"benchy{u}_0.2mm_PLA_MK4.gcode")
        assert r.status_code == 201 and r.json()["done"] is True and r.json()["files"]["local"]["name"] == f"benchy{u}_0.2mm_PLA_MK4.gcode"
        kept = authed.get("/api/print-files", params={"model_id": m["id"]}).json()
        assert len(kept) == 1 and kept[0]["filename"] == f"benchy{u}_0.2mm_PLA_MK4.gcode" and kept[0]["est_grams"] == 12.5 and kept[0]["est_minutes"] == 40 and kept[0]["layer_height"] == "0.2"
        assert kept[0]["notes"] == "Sent from the slicer" and authed.get("/api/queue").json() == []                          # "upload" does not queue
        inbox = authed.get("/api/slicer-inbox").json()
        assert inbox["waiting"] == 0 and inbox["files"][0]["status"] == "filed" and inbox["files"][0]["model"] == f"benchy{u}.stl" and inbox["files"][0]["sent_by"] == "token:slicer"
        again = _send(key, f"benchy{u}_other.gcode")                                                                              # the same file again: one kept copy
        assert again.status_code == 201 and len(authed.get("/api/print-files", params={"model_id": m["id"]}).json()) == 1
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_upload_and_print_queues_it_and_never_starts_anything(authed, key):
    u = _u()
    m = _model(authed, f"bracket{u}.stl")
    try:
        r = _send(key, f"bracket{u}_PETG.gcode", print="true")
        assert r.status_code == 201
        queue = authed.get("/api/queue").json()
        assert len(queue) == 1 and queue[0]["model_id"] == m["id"] and queue[0]["status"] == "queued" and queue[0]["estimated_grams"] == 12.5 and queue[0]["printer_id"] is None
        assert queue[0]["notes"] == "Sent from the slicer"
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_the_longest_matching_name_wins_and_an_unclear_name_waits(authed, key):
    u = _u()
    plain, small = _model(authed, f"gear{u}.stl"), _model(authed, f"gear{u} small.stl")
    twin_a, twin_b = _model(authed, f"twin{u}.stl"), _model(authed, f"twin{u}.3mf")
    try:
        _send(key, f"gear{u}_small_0.2mm.gcode")
        assert len(authed.get("/api/print-files", params={"model_id": small["id"]}).json()) == 1 and authed.get("/api/print-files", params={"model_id": plain["id"]}).json() == []
        r = _send(key, f"twin{u}_0.2mm.gcode", GCODE + b"; other\n")
        assert r.status_code == 201
        waiting = [f for f in authed.get("/api/slicer-inbox").json()["files"] if f["status"] == "waiting"]
        assert [f["filename"] for f in waiting] == [f"twin{u}_0.2mm.gcode"]                                                    # two models fit equally: it asks
        assert authed.get("/api/print-files", params={"model_id": twin_a["id"]}).json() == [] and authed.get("/api/print-files", params={"model_id": twin_b["id"]}).json() == []
        _send(key, "zz.gcode", GCODE + b"; short name\n")
        assert [f["filename"] for f in authed.get("/api/slicer-inbox").json()["files"] if f["status"] == "waiting"][0] == "zz.gcode"          # a name too short to trust
    finally:
        for m in (plain, small, twin_a, twin_b):
            authed.delete(f"/api/library/models/{m['id']}")


def test_a_waiting_file_can_be_filed_by_hand_once(authed, key):
    u = _u()
    m = _model(authed, f"tool holder{u}.stl")
    try:
        _send(key, f"mystery{u}.gcode", print="true")
        waiting = authed.get("/api/slicer-inbox").json()
        assert waiting["waiting"] == 1
        up = waiting["files"][0]
        assert authed.post(f"/api/slicer-inbox/{up['id']}/file", json={"model_id": 987654}).status_code == 400
        assert authed.post(f"/api/slicer-inbox/{up['id']}/file", json={}).status_code == 400
        filed = authed.post(f"/api/slicer-inbox/{up['id']}/file", json={"model_id": m["id"]}).json()
        assert filed["status"] == "filed" and filed["model"] == f"tool holder{u}.stl"
        assert len(authed.get("/api/print-files", params={"model_id": m["id"]}).json()) == 1 and len(authed.get("/api/queue").json()) == 1       # it had asked to print
        assert authed.post(f"/api/slicer-inbox/{up['id']}/file", json={"model_id": m["id"]}).status_code == 409
        assert authed.post("/api/slicer-inbox/999999/file", json={"model_id": m["id"]}).status_code == 404
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_waiting_sliced_3mf_can_become_a_model_but_gcode_cannot(authed, key):
    _send(key, f"project_{uuid.uuid4().hex[:5]}.3mf", _3mf())
    _send(key, "loose_notes_x.gcode", GCODE + b"; again\n")
    files = {f["kind"]: f for f in authed.get("/api/slicer-inbox").json()["files"] if f["status"] == "waiting"}
    assert set(files) == {"3mf", "gcode"}
    assert authed.post(f"/api/slicer-inbox/{files['gcode']['id']}/new-model").status_code == 400
    made = authed.post(f"/api/slicer-inbox/{files['3mf']['id']}/new-model")
    assert made.status_code == 200, made.text
    mid = made.json()["model_id"]
    try:
        assert len(authed.get("/api/print-files", params={"model_id": mid}).json()) == 1
        assert authed.get(f"/api/library/models/{mid}").json()["extension"] == ".3mf"
    finally:
        authed.delete(f"/api/library/models/{mid}")


def test_bad_files_are_refused_and_names_are_cleaned(authed, key):
    assert _send(key, "x.exe", b"MZ").status_code == 415 and _send(key, "notes.txt", b"hello").status_code == 415
    assert _send(key, "empty.gcode", b"").status_code == 400
    r = _send(key, "../../etc/evil_name.gcode")
    assert r.status_code == 201 and r.json()["files"]["local"]["name"] == "evil_name.gcode"
    assert _client().post("/api/files/local", headers={"X-Api-Key": key}, data={"print": "false"}).status_code == 422                 # no file at all


def test_deleting_a_waiting_file_removes_it_from_the_disk(authed, key):
    from sqlmodel import Session
    from app import print_files
    from app.db import engine
    _send(key, "to_delete_xyz.gcode", GCODE + b"; delete me\n")
    up = authed.get("/api/slicer-inbox").json()["files"][0]
    with Session(engine) as s:
        path = print_files.stored_path(s.get(SlicerUpload, up["id"]).stored_name)
    assert path.is_file()
    assert authed.delete(f"/api/slicer-inbox/{up['id']}").status_code == 200 and not path.exists()
    assert authed.delete(f"/api/slicer-inbox/{up['id']}").status_code == 404


# ---------------------------------------------------------------- Moonraker uploads
def test_the_moonraker_interface_answers_and_accepts_uploads(authed, key):
    c = _client()
    headers = {"X-Api-Key": key}
    assert c.get("/server/info").status_code == 401
    info = c.get("/server/info", headers=headers).json()["result"]
    assert info["klippy_state"] == "ready" and info["klippy_connected"] is True and info["api_version"]
    assert c.get("/printer/info", headers=headers).json()["result"]["state"] == "ready"
    u = _u()
    m = _model(authed, f"vase{u}.stl")
    try:
        r = _send(key, f"vase{u}_0.2.gcode", path="/server/files/upload", root="gcodes", print="true")
        assert r.status_code == 200
        body = r.json()
        assert body["item"]["path"] == f"vase{u}_0.2.gcode" and body["item"]["root"] == "gcodes" and body["print_started"] is False and body["print_queued"] is True
        assert len(authed.get("/api/print-files", params={"model_id": m["id"]}).json()) == 1 and len(authed.get("/api/queue").json()) == 1
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_the_controls_are_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    for element in ("virtual-printer-box", "slicer-inbox-box"):
        assert f'id="{element}"' in html, element
    for needle in ("/api/settings/virtual-printer", "/api/slicer-inbox", "loadSlicerInbox", "new-model"):
        assert needle in js, needle
