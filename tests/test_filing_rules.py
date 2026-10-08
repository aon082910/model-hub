"""Filing rules: tag or file a model automatically from what its listing says."""
import json
import uuid

import httpx
import pytest
from starlette.testclient import TestClient

from app import filing, sources
from test_sources import FakeSites

PW = "a long enough password"


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture(autouse=True)
def sites(monkeypatch, authed):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    yield fake
    for r in authed.get("/api/filing-rules").json()["rules"]:
        authed.delete(f"/api/filing-rules/{r['id']}")


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"fr_{uuid.uuid4().hex[:6]}.stl", _stl(40 + uuid.uuid4().int % 90), "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}/source")
    authed.delete(f"/api/library/models/{m['id']}")


def _rule(c, **fields):
    payload = {"field": "category", "match": "test models", "action": "add_tag", "value": "Calibration", **fields}
    r = c.post("/api/filing-rules", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _link(c, m):
    r = c.post(f"/api/library/models/{m['id']}/source", json={"provider": "printables", "source_id": "3161", "images": False})
    assert r.status_code == 200, r.text
    return r.json()


def _tags(c, m):
    return sorted(t["name"] for t in c.get(f"/api/library/models/{m['id']}").json()["tags"])


def _collections(c, m):
    return sorted(x["name"] for x in c.get(f"/api/library/models/{m['id']}/full").json()["collections"])


# ---------- matching ----------

def _model_like(**kw):
    from app.models import Model3D
    base = dict(id=1, filename="gear_v2.stl", path="x", extension=".stl", size_bytes=1, content_hash="h", source_category="Test models",
                source_tags=json.dumps(["Benchy", "Boat"]), source_title="3D BENCHY", designer="Prusa Research")
    base.update(kw)
    return Model3D(**base)


def test_rules_match_text_inside_the_chosen_field_ignoring_case():
    from app.models import FilingRule
    m = _model_like()
    for field, text, expected in (("category", "TEST", True), ("category", "vase", False), ("tag", "boa", True), ("tag", "ship", False),
                                  ("title", "benchy", True), ("title", "gear_v2", True), ("designer", "prusa", True), ("designer", "bambu", False)):
        assert filing.rule_matches(FilingRule(field=field, match=text, action="add_tag", value="x"), m) is expected, (field, text)
    assert filing.rule_matches(FilingRule(field="category", match=" ", action="add_tag", value="x"), m) is False
    assert filing.rule_matches(FilingRule(field="category", match="x", action="add_tag", value="x"), _model_like(source_category=None, source_tags="not json")) is False


# ---------- the rules API ----------

def test_rules_are_created_listed_edited_and_deleted(authed):
    r = _rule(authed, value="  Mixed Case TAG  ")
    assert r["value"] == "mixed case tag" and r["enabled"] is True                         # tags are lower-cased
    c = _rule(authed, field="tag", match="boat", action="add_collection", value="  Boats ")
    assert c["value"] == "Boats"                                                              # collection names keep their case
    listing = authed.get("/api/filing-rules").json()
    assert [x["id"] for x in listing["rules"]] == [r["id"], c["id"]] and "category" in listing["fields"]
    assert authed.patch(f"/api/filing-rules/{r['id']}", json={"enabled": False, "match": "other text"}).json()["enabled"] is False
    assert authed.delete(f"/api/filing-rules/{r['id']}").status_code == 200
    assert authed.delete(f"/api/filing-rules/{r['id']}").status_code == 404
    assert authed.patch("/api/filing-rules/987654", json={}).status_code == 404


@pytest.mark.parametrize("payload", [{"field": "colour"}, {"match": "x"}, {"match": "y" * 81}, {"match": 5}, {"action": "explode"}, {"value": ""}, {"value": 5}])
def test_bad_rules_are_refused(authed, payload):
    base = {"field": "category", "match": "valid", "action": "add_tag", "value": "ok"}
    assert authed.post("/api/filing-rules", json={**base, **payload}).status_code == 400
    assert authed.get("/api/filing-rules").json()["rules"] == []


def test_enabled_must_be_a_boolean(authed):
    r = _rule(authed)
    assert authed.patch(f"/api/filing-rules/{r['id']}", json={"enabled": "no"}).status_code == 400


# ---------- running them ----------

def test_linking_a_model_applies_the_rules(authed, model):
    _rule(authed, field="category", match="test models", action="add_tag", value="calibration")
    _rule(authed, field="tag", match="boat", action="add_collection", value="Boats from listings")
    _rule(authed, field="designer", match="nobody-matches", action="add_tag", value="never")
    _link(authed, model)
    assert _tags(authed, model) == ["calibration"] and _collections(authed, model) == ["Boats from listings"]
    entry = authed.get("/api/activity", params={"limit": 3, "action": "filing"}).json()["items"][0]
    assert entry["actor"] == "filing rules" and entry["undoable"] is True
    assert authed.post(f"/api/activity/{entry['id']}/undo").status_code == 200
    assert _tags(authed, model) == [] and _collections(authed, model) == []
    for c in authed.get("/api/collections").json():
        if c["name"] == "Boats from listings":
            authed.delete(f"/api/collections/{c['id']}")


def test_a_disabled_rule_does_nothing_and_nothing_is_filed_without_rules(authed, model):
    r = _rule(authed)
    authed.patch(f"/api/filing-rules/{r['id']}", json={"enabled": False})
    _link(authed, model)
    assert _tags(authed, model) == []
    assert authed.post("/api/filing-rules/apply", json={}).json()["rules"] == 0


def test_a_failing_rule_never_breaks_the_link(authed, model, monkeypatch):
    _rule(authed)
    monkeypatch.setattr(filing, "apply", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    linked = _link(authed, model)
    assert linked["source_provider"] == "printables"


def test_the_category_is_remembered_and_cleared_on_unlink(authed, model):
    _link(authed, model)
    assert authed.get(f"/api/library/models/{model['id']}").json()["source_category"] == "Test models"
    authed.delete(f"/api/library/models/{model['id']}/source")
    assert authed.get(f"/api/library/models/{model['id']}").json()["source_category"] is None


def test_applying_to_every_linked_model_has_a_dry_run_and_is_undoable(authed, model):
    _link(authed, model)                                                                # linked before any rule exists
    assert _tags(authed, model) == []
    _rule(authed, value="added-later")
    dry = authed.post("/api/filing-rules/apply", json={"dry_run": True}).json()
    assert dry["dry_run"] is True and dry["changed"] >= 1 and dry["activity_id"] is None and _tags(authed, model) == []
    done = authed.post("/api/filing-rules/apply", json={}).json()
    assert done["changed"] >= 1 and done["activity_id"] and _tags(authed, model) == ["added-later"]
    again = authed.post("/api/filing-rules/apply", json={}).json()
    assert again["changed"] == 0 and again["activity_id"] is None                       # nothing twice
    assert authed.post(f"/api/activity/{done['activity_id']}/undo").json()["restored"] >= 1
    assert _tags(authed, model) == []


def test_a_dry_run_never_creates_collections(authed, model):
    _link(authed, model)
    _rule(authed, action="add_collection", value=f"Dry only {model['id']}")
    result = authed.post("/api/filing-rules/apply", json={"dry_run": True}).json()
    assert result["changed"] >= 1
    assert not [c for c in authed.get("/api/collections").json() if c["name"] == f"Dry only {model['id']}"]
    authed.post("/api/filing-rules/apply", json={})
    made = [c for c in authed.get("/api/collections").json() if c["name"] == f"Dry only {model['id']}"]
    assert len(made) == 1
    authed.delete(f"/api/collections/{made[0]['id']}")


def test_viewers_cannot_change_rules(authed):
    authed.post("/api/users", json={"username": "ruleviewer", "password": PW, "role": "viewer"})
    from app.main import app
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "ruleviewer", "password": PW})
        assert viewer.get("/api/filing-rules").status_code == 200
        assert viewer.post("/api/filing-rules", json={"field": "tag", "match": "xx", "action": "add_tag", "value": "y"}).status_code == 403
        assert viewer.post("/api/filing-rules/apply", json={}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    assert TestClient(app).get("/api/filing-rules").status_code == 401
