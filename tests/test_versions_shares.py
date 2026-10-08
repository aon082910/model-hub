"""Versions of a model grouped together, read-only share links, and QR codes."""
import io
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

from app.config import DB_PATH

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


@pytest.fixture()
def trio(authed):
    tag = uuid.uuid4().hex[:6]
    a = _import(authed, f"{tag}_bracket_v1.stl", _stl(40 + int(tag[:2], 16) % 20))
    b = _import(authed, f"{tag}_bracket_v2.stl", _stl(61 + int(tag[:2], 16) % 20))
    c = _import(authed, f"{tag}_unrelated.stl", _stl(83 + int(tag[:2], 16) % 20, z=3))
    yield tag, a, b, c
    for m in (a, b, c):
        authed.delete(f"/api/library/models/{m['id']}")


# ---------- versions ----------

def test_models_are_grouped_labelled_and_shown_on_their_page(authed, trio):
    tag, a, b, c = trio
    r = authed.post("/api/families", json={"model_ids": [a["id"], b["id"]], "name": "Bracket"})
    assert r.status_code == 200, r.text
    fam = r.json()
    assert fam["name"] == "Bracket" and sorted(m["id"] for m in fam["members"]) == sorted([a["id"], b["id"]])
    assert authed.put(f"/api/families/members/{a['id']}", json={"label": "  v1 "}).json()["version_label"] == "v1"
    authed.put(f"/api/families/members/{b['id']}", json={"label": "v2 (final)"})
    page = authed.get(f"/api/library/models/{a['id']}/full").json()
    assert page["family"]["name"] == "Bracket"
    assert {m["id"]: m["version_label"] for m in page["family"]["members"]} == {a["id"]: "v1", b["id"]: "v2 (final)"}
    assert authed.get(f"/api/library/models/{c['id']}/full").json()["family"] is None
    assert authed.patch(f"/api/families/{fam['id']}", json={"name": "Renamed"}).json()["name"] == "Renamed"


def test_leaving_dissolves_a_family_of_one_and_deleting_a_model_does_too(authed, trio):
    tag, a, b, c = trio
    fam = authed.post("/api/families", json={"model_ids": [a["id"], b["id"], c["id"]]}).json()
    assert authed.delete(f"/api/families/members/{c['id']}").status_code == 200
    assert len(authed.get(f"/api/families/{fam['id']}").json()["members"]) == 2
    authed.delete(f"/api/library/models/{b['id']}")                         # the family is down to one model
    assert authed.get(f"/api/families/{fam['id']}").status_code == 404
    assert authed.get(f"/api/library/models/{a['id']}/full").json()["family"] is None


def test_grouping_into_an_existing_family_joins_families(authed, trio):
    tag, a, b, c = trio
    first = authed.post("/api/families", json={"model_ids": [a["id"], b["id"]]}).json()
    joined = authed.post("/api/families", json={"model_ids": [b["id"], c["id"]]}).json()
    assert joined["id"] == first["id"] and len(joined["members"]) == 3


def test_suggestions_find_same_name_and_same_shape(authed, trio):
    tag, a, b, c = trio
    same_shape = _import(authed, f"{tag}_other_name.stl", _stl(40 + int(tag[:2], 16) % 20) + b"\n")
    try:
        found = {s["id"]: s for s in authed.get(f"/api/families/suggest/{a['id']}").json()["suggestions"]}
        assert b["id"] in found and c["id"] not in found                       # v1 and v2 look like versions of each other
        authed.post("/api/families", json={"model_ids": [a["id"], b["id"]]})
        assert b["id"] not in {s["id"] for s in authed.get(f"/api/families/suggest/{a['id']}").json()["suggestions"]}   # already together
    finally:
        authed.delete(f"/api/library/models/{same_shape['id']}")
    assert authed.get("/api/families/suggest/987654").status_code == 404


def test_latest_only_hides_older_versions(authed, trio):
    tag, a, b, c = trio
    authed.post("/api/families", json={"model_ids": [a["id"], b["id"]]})
    everything = [m["filename"] for m in authed.get("/api/library/models", params={"q": tag, "limit": 5000}).json()]
    latest = [m["filename"] for m in authed.get("/api/library/models", params={"q": tag, "latest_only": "true", "limit": 5000}).json()]
    assert a["filename"] in everything and a["filename"] not in latest
    assert b["filename"] in latest and c["filename"] in latest


@pytest.mark.parametrize("payload,status", [({}, 400), ({"model_ids": [1]}, 400), ({"model_ids": "1,2"}, 400), ({"model_ids": [1, "2"]}, 400),
                                            ({"model_ids": [987654, 987655]}, 404)])
def test_bad_groupings_are_refused(authed, payload, status):
    assert authed.post("/api/families", json=payload).status_code == status


def test_labels_need_a_family(authed, trio):
    assert authed.put(f"/api/families/members/{trio[1]['id']}", json={"label": "x"}).status_code == 400
    assert authed.put("/api/families/members/987654", json={"label": "x"}).status_code == 404


# ---------- share links ----------

def _share(c, **fields):
    r = c.post("/api/shares", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_model_share_is_public_read_only_and_leaves_private_things_out(authed, trio):
    tag, a, b, c = trio
    authed.patch(f"/api/library/models/{a['id']}", json={"designer": "Dana <b>Maker</b>", "license": "CC0", "notes": "PRIVATE NOTE TEXT"})
    authed.post(f"/api/tags/models/{a['id']}", json={"name": "sharetag"})
    authed.put(f"/api/library/models/{a['id']}/print-settings", json={"material": "PETG", "notes": "slow it down"})
    authed.post("/api/prints", json={"model_id": a["id"], "notes": "PRIVATE PRINT LOG"})
    link = _share(authed, kind="model", target_id=a["id"])
    assert link["path"].startswith("/share/") and link["target_name"] == a["filename"] and link["allow_downloads"] is False
    r = _anonymous().get(link["path"])
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    page = r.text
    assert a["filename"] in page and "sharetag" in page and "PETG" in page and "slow it down" in page and "CC0" in page
    assert "Dana &lt;b&gt;Maker&lt;/b&gt;" in page and "<b>Maker</b>" not in page              # escaped
    assert "PRIVATE NOTE TEXT" not in page and "PRIVATE PRINT LOG" not in page
    assert "Download file" not in page
    assert "default-src 'none'" in r.headers["content-security-policy"] and r.headers["x-robots-tag"].startswith("noindex")
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"


def test_markup_in_names_cannot_run_on_the_share_page(authed, trio):
    tag, a, b, c = trio
    authed.patch(f"/api/library/models/{a['id']}", json={"designer": "<script>alert(1)</script>", "source_url": "javascript:alert(1)"})
    project = authed.post("/api/projects", json={"name": "<img src=x onerror=alert(1)> project", "description": "<script>bad()</script>"}).json()
    try:
        for kind, target in (("model", a["id"]), ("project", project["id"])):
            page = _anonymous().get(_share(authed, kind=kind, target_id=target)["path"]).text
            assert "<script>" not in page and "<img src=x" not in page and "javascript:alert" not in page
    finally:
        authed.delete(f"/api/projects/{project['id']}")


def test_downloads_are_only_available_when_switched_on(authed, trio):
    tag, a, b, c = trio
    closed = _share(authed, kind="model", target_id=a["id"])
    open_ = _share(authed, kind="model", target_id=a["id"], allow_downloads=True)
    anon = _anonymous()
    assert anon.get(f"{closed['path']}/file/{a['id']}").status_code == 404
    r = anon.get(f"{open_['path']}/file/{a['id']}")
    assert r.status_code == 200 and b"solid t" in r.content
    assert "Download file" in anon.get(open_["path"]).text
    assert anon.get(f"{open_['path']}/file/{b['id']}").status_code == 404                  # only what the link shows


def test_a_project_share_shows_its_models_and_parts_but_costs_only_when_asked(authed, trio):
    tag, a, b, c = trio
    project = authed.post("/api/projects", json={"name": f"Shared project {tag}", "description": "Public description", "notes": "PRIVATE PROJECT NOTES"}).json()
    authed.post(f"/api/projects/{project['id']}/models/{a['id']}")
    authed.post(f"/api/projects/{project['id']}/parts", json={"name": "Sharable ESP32", "quantity": 2, "unit_cost": 6.5})
    try:
        plain = _anonymous().get(_share(authed, kind="project", target_id=project["id"])["path"]).text
        assert "Public description" in plain and a["filename"] in plain and "Sharable ESP32" in plain
        assert "PRIVATE PROJECT NOTES" not in plain and "6.50" not in plain and c["filename"] not in plain
        priced = _anonymous().get(_share(authed, kind="project", target_id=project["id"], show_costs=True)["path"]).text
        assert "$6.50" in priced
    finally:
        authed.delete(f"/api/projects/{project['id']}")


def test_pictures_only_for_models_the_link_shows(authed, trio):
    tag, a, b, c = trio
    link = _share(authed, kind="model", target_id=a["id"])
    anon = _anonymous()
    assert anon.get(f"{link['path']}/thumb/{b['id']}").status_code == 404
    assert anon.get(f"{link['path']}/thumb/{a['id']}").status_code in (200, 404)             # 404 only if no picture exists


def test_bad_unknown_expired_and_revoked_links_all_look_the_same(authed, trio):
    tag, a, b, c = trio
    anon = _anonymous()
    assert anon.get("/share/not-a-real-token-at-all-0000").status_code == 404
    assert anon.get("/share/short").status_code == 404
    link = _share(authed, kind="model", target_id=a["id"], expires_days=1)
    assert anon.get(link["path"]).status_code == 200
    from sqlmodel import Session, select
    from app.db import engine
    from app.models import ShareLink
    with Session(engine) as s:
        row = s.exec(select(ShareLink).where(ShareLink.id == link["id"])).first()
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        s.add(row)
        s.commit()
    assert anon.get(link["path"]).status_code == 404
    assert next(l for l in authed.get("/api/shares").json() if l["id"] == link["id"])["expired"] is True
    fresh = _share(authed, kind="model", target_id=a["id"])
    assert authed.delete(f"/api/shares/{fresh['id']}").status_code == 200
    assert anon.get(fresh["path"]).status_code == 404
    assert authed.delete(f"/api/shares/{fresh['id']}").status_code == 404


def test_a_deleted_target_ends_its_links_for_good(authed, trio):
    tag, a, b, c = trio
    link = _share(authed, kind="model", target_id=c["id"])
    authed.delete(f"/api/library/models/{c['id']}")
    assert _anonymous().get(link["path"]).status_code == 404
    assert not [l for l in authed.get("/api/shares").json() if l["id"] == link["id"]]            # the row is gone, not just hidden
    project = authed.post("/api/projects", json={"name": "short lived"}).json()
    plink = _share(authed, kind="project", target_id=project["id"])
    authed.delete(f"/api/projects/{project['id']}")
    assert _anonymous().get(plink["path"]).status_code == 404
    assert not [l for l in authed.get("/api/shares").json() if l["id"] == plink["id"]]


@pytest.mark.parametrize("payload", [{}, {"kind": "tag", "target_id": 1}, {"kind": "model", "target_id": "1"}, {"kind": "model", "target_id": True},
                                     {"kind": "model", "target_id": 987654}, {"kind": "project", "target_id": 987654},
                                     {"kind": "model", "target_id": 1, "expires_days": 0}, {"kind": "model", "target_id": 1, "expires_days": "soon"}])
def test_bad_shares_are_refused(authed, payload):
    assert authed.post("/api/shares", json=payload).status_code in (400, 404)


def test_viewers_cannot_see_or_make_links_but_members_can(authed, trio):
    tag, a, b, c = trio
    authed.post("/api/users", json={"username": "shareviewer", "password": PW, "role": "viewer"})
    authed.post("/api/users", json={"username": "sharemember", "password": PW, "role": "member"})
    try:
        viewer, member = _anonymous(), _anonymous()
        assert viewer.post("/api/auth/login", json={"username": "shareviewer", "password": PW}).status_code == 200
        assert member.post("/api/auth/login", json={"username": "sharemember", "password": PW}).status_code == 200
        assert viewer.get("/api/shares").status_code == 403 and viewer.post("/api/shares", json={"kind": "model", "target_id": a["id"]}).status_code == 403
        made = member.post("/api/shares", json={"kind": "model", "target_id": a["id"]})
        assert made.status_code == 200 and made.json()["created_by"] == "sharemember"
        assert member.get("/api/shares").status_code == 200
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert _anonymous().get("/api/shares").status_code == 401


def test_share_links_are_not_part_of_a_backup_or_replaced_by_a_restore(authed, trio):
    tag, a, b, c = trio
    link = _share(authed, kind="model", target_id=a["id"])
    data = authed.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extract("modelhub.db", path=str(DB_PATH.parent / "peek_shares"))
    conn = sqlite3.connect(DB_PATH.parent / "peek_shares" / "modelhub.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM sharelink").fetchone()[0] == 0
    finally:
        conn.close()
    assert link["path"].split("/")[-1].encode() not in data
    assert authed.post("/api/backup/restore", files={"file": ("b.zip", data)}, data={"confirm": "replace"}).status_code == 200
    assert _anonymous().get(link["path"]).status_code == 200                                   # still works
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


def test_listing_shares_for_one_target(authed, trio):
    tag, a, b, c = trio
    _share(authed, kind="model", target_id=a["id"])
    _share(authed, kind="model", target_id=b["id"])
    mine = authed.get("/api/shares", params={"kind": "model", "target_id": a["id"]}).json()
    assert len(mine) == 1 and mine[0]["target_id"] == a["id"]


# ---------- QR codes ----------

def test_qr_codes_are_svg_and_bounded(authed):
    r = authed.get("/api/qr", params={"text": "http://192.168.1.50:8420/#/spool/7"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml" and r.text.startswith("<svg")
    assert authed.get("/api/qr", params={"text": "x" * 301}).status_code == 400
    assert authed.get("/api/qr", params={"text": ""}).status_code == 422
    assert authed.get("/api/qr").status_code == 422
    assert _anonymous().get("/api/qr", params={"text": "x"}).status_code == 401
