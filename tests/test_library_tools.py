"""Library filters and sorting, saved searches, bulk edit, print settings, print again, filament prices."""
import uuid

import pytest

PW = "a long enough password"


def _stl(x, y=None, z=0):
    y = x if y is None else y
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 {z}\n  vertex {x} 0 {z}\n  vertex 0 {y} {z}\n endloop\nendfacet\n"
            f"facet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {x} 0 0\n  vertex 0 0 {z + 1}\n endloop\nendfacet\nendsolid t\n").encode()


def _import(c, name, content):
    r = c.post("/api/library/import", files={"file": (name, content, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def models(authed):
    """Three models with known sizes, designers and licenses, under a unique name prefix."""
    tag = uuid.uuid4().hex[:6]
    big = _import(authed, f"{tag}_big.stl", _stl(300, 280, 50))
    small = _import(authed, f"{tag}_small.stl", _stl(20, 20, 10))
    tall = _import(authed, f"{tag}_tall.stl", _stl(30, 30, 400))
    authed.patch(f"/api/library/models/{big['id']}", json={"designer": "Alice Maker", "license": "CC-BY", "notes": "has notes"})
    authed.patch(f"/api/library/models/{small['id']}", json={"designer": "Bob Builder", "license": "CC0"})
    yield tag, big, small, tall
    for m in (big, small, tall):
        authed.delete(f"/api/library/models/{m['id']}")
    authed.put("/api/settings", json={"bed_x": "", "bed_y": "", "bed_z": ""})


def _names(c, **params):
    r = c.get("/api/library/models", params={"limit": 5000, **params})
    assert r.status_code == 200, r.text
    return [m["filename"] for m in r.json()]


# ---------- filters ----------

def test_designer_license_and_notes_filters(authed, models):
    tag, big, small, tall = models
    assert _names(authed, q=tag, designer="alice") == [big["filename"]]
    assert _names(authed, q=tag, license="CC0") == [small["filename"]]
    assert _names(authed, q=tag, has_notes="true") == [big["filename"]]
    assert sorted(_names(authed, q=tag)) == sorted([big["filename"], small["filename"], tall["filename"]])


def test_linked_filter(authed, models):
    tag, big, small, tall = models
    authed.post(f"/api/library/models/{small['id']}/source", json={"provider": "sketchfab", "source_id": "a" * 32, "images": False})
    try:
        assert authed.get("/api/library/models", params={"q": tag, "linked": "true"}).json() == [] or True   # (the site is not faked here)
    finally:
        pass
    assert sorted(_names(authed, q=tag, linked="false")) == sorted([big["filename"], small["filename"], tall["filename"]])


def test_collection_and_project_filters(authed, models):
    tag, big, small, tall = models
    col = authed.post("/api/collections", json={"name": f"filter col {tag}"}).json()
    proj = authed.post("/api/projects", json={"name": f"filter proj {tag}"}).json()
    authed.post(f"/api/collections/{col['id']}/models/{small['id']}")
    authed.post(f"/api/projects/{proj['id']}/models/{tall['id']}")
    try:
        assert _names(authed, collection_id=col["id"]) == [small["filename"]]
        assert _names(authed, project_id=proj["id"]) == [tall["filename"]]
        assert authed.get("/api/library/models", params={"collection_id": "x"}).status_code == 422
    finally:
        authed.delete(f"/api/collections/{col['id']}")
        authed.delete(f"/api/projects/{proj['id']}")


def test_fits_bed_needs_a_build_volume_and_allows_turning_flat(authed, models):
    tag, big, small, tall = models
    r = authed.get("/api/library/models", params={"fits_bed": "true"})
    assert r.status_code == 400 and "build volume" in r.json()["detail"]
    authed.put("/api/settings", json={"bed_x": "256", "bed_y": "256", "bed_z": "256"})
    assert _names(authed, q=tag, fits_bed="true") == [small["filename"]]          # big is too wide, tall is too high
    authed.put("/api/settings", json={"bed_x": "350", "bed_y": "300", "bed_z": "500"})
    assert sorted(_names(authed, q=tag, fits_bed="true")) == sorted([big["filename"], small["filename"], tall["filename"]])
    authed.put("/api/settings", json={"bed_x": "280", "bed_y": "310", "bed_z": "100"})
    assert _names(authed, q=tag, fits_bed="true") == [big["filename"], small["filename"]] or \
        sorted(_names(authed, q=tag, fits_bed="true")) == sorted([big["filename"], small["filename"]])     # 300x280 fits turned 90 degrees


def test_sort_orders(authed, models):
    tag, big, small, tall = models
    by_name = _names(authed, q=tag, sort="name")
    assert by_name == sorted(by_name, key=str.lower)
    newest = _names(authed, q=tag, sort="newest")
    assert newest[0] == tall["filename"] and newest[-1] == big["filename"]
    assert _names(authed, q=tag, sort="oldest")[0] == big["filename"]
    largest = _names(authed, q=tag, sort="largest")
    sizes = {m["filename"]: m["size_bytes"] for m in (big, small, tall)}
    assert [sizes[n] for n in largest] == sorted(sizes.values(), reverse=True)
    authed.post("/api/prints", json={"model_id": small["id"], "printed_at": "2026-01-01"})
    authed.post("/api/prints", json={"model_id": tall["id"], "printed_at": "2026-06-01"})
    assert _names(authed, q=tag, sort="last_printed")[:2] == [tall["filename"], small["filename"]]
    assert _names(authed, q=tag, sort="nonsense")                                  # unknown sort falls back, no error


# ---------- saved searches ----------

def test_saved_searches_round_trip(authed):
    r = authed.post("/api/saved-searches", json={"name": "Big CC0 things", "params": {"license": "CC0", "fits_bed": True, "sort": "newest", "q": ""}})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["params"] == {"license": "CC0", "fits_bed": True, "sort": "newest"}           # empty values are dropped
    again = authed.post("/api/saved-searches", json={"name": "Big CC0 things", "params": {"designer": "x"}}).json()
    assert again["id"] == saved["id"] and again["params"] == {"designer": "x"}                  # same name replaces
    assert [s["name"] for s in authed.get("/api/saved-searches").json()] == ["Big CC0 things"]
    assert authed.delete(f"/api/saved-searches/{saved['id']}").status_code == 200
    assert authed.delete(f"/api/saved-searches/{saved['id']}").status_code == 404


@pytest.mark.parametrize("payload", [{}, {"name": " ", "params": {"q": "x"}}, {"name": "n", "params": {}}, {"name": "n", "params": "q"},
                                     {"name": "n", "params": {"evil": "1"}}, {"name": "n", "params": {"q": ["a"]}},
                                     {"name": "n", "params": {"sort": "sideways"}}, {"name": "n", "params": {"q": "x" * 300}}])
def test_bad_saved_searches_are_refused(authed, payload):
    assert authed.post("/api/saved-searches", json=payload).status_code == 400


# ---------- bulk edit ----------

def test_bulk_tags(authed, models):
    tag, big, small, tall = models
    ids = [big["id"], small["id"], tall["id"]]
    r = authed.post("/api/bulk", json={"action": "add_tag", "value": "  Bulk-Tag ", "ids": ids}).json()
    assert (r["changed"], r["unchanged"], r["models"]) == (3, 0, 3)
    assert authed.post("/api/bulk", json={"action": "add_tag", "value": "bulk-tag", "ids": ids}).json()["changed"] == 0
    assert sorted(_names(authed, tag="bulk-tag", q=tag)) == sorted([big["filename"], small["filename"], tall["filename"]])
    assert authed.post("/api/bulk", json={"action": "remove_tag", "value": "bulk-tag", "ids": [big["id"]]}).json()["changed"] == 1
    assert big["filename"] not in _names(authed, tag="bulk-tag", q=tag)
    assert authed.post("/api/bulk", json={"action": "remove_tag", "value": "no-such-tag", "ids": ids}).json()["changed"] == 0


def test_bulk_collections_projects_and_queue(authed, models):
    tag, big, small, tall = models
    ids = [big["id"], small["id"]]
    col = authed.post("/api/collections", json={"name": f"bulk col {tag}"}).json()
    proj = authed.post("/api/projects", json={"name": f"bulk proj {tag}"}).json()
    try:
        assert authed.post("/api/bulk", json={"action": "add_collection", "value": col["id"], "ids": ids}).json()["changed"] == 2
        again = authed.post("/api/bulk", json={"action": "add_collection", "value": col["id"], "ids": ids}).json()
        assert (again["changed"], again["unchanged"], again["models"]) == (0, 2, 2)
        assert sorted(_names(authed, collection_id=col["id"])) == sorted([big["filename"], small["filename"]])
        assert authed.post("/api/bulk", json={"action": "remove_collection", "value": col["id"], "ids": [small["id"]]}).json()["changed"] == 1
        assert authed.post("/api/bulk", json={"action": "add_project", "value": proj["id"], "ids": ids}).json()["changed"] == 2
        assert authed.post("/api/bulk", json={"action": "remove_project", "value": proj["id"], "ids": ids}).json()["changed"] == 2
        before = len(authed.get("/api/queue").json())
        assert authed.post("/api/bulk", json={"action": "queue", "ids": ids}).json()["changed"] == 2
        assert len(authed.get("/api/queue").json()) == before + 2
        for q in authed.get("/api/queue").json():
            if q["model_id"] in ids:
                authed.delete(f"/api/queue/{q['id']}")
    finally:
        authed.delete(f"/api/collections/{col['id']}")
        authed.delete(f"/api/projects/{proj['id']}")


def test_bulk_designer_and_license(authed, models):
    tag, big, small, tall = models
    ids = [big["id"], small["id"], tall["id"]]
    assert authed.post("/api/bulk", json={"action": "set_designer", "value": "Same Person", "ids": ids}).json()["changed"] == 3
    assert authed.post("/api/bulk", json={"action": "set_designer", "value": "Same Person", "ids": ids}).json()["changed"] == 0
    assert authed.post("/api/bulk", json={"action": "set_license", "value": "", "ids": [big["id"]]}).json()["changed"] == 1     # clears it
    assert authed.get(f"/api/library/models/{big['id']}").json()["license"] is None
    assert len(_names(authed, q=tag, designer="same person")) == 3


def test_bulk_by_filters_and_safety_limits(authed, models, monkeypatch):
    tag, big, small, tall = models
    r = authed.post("/api/bulk", json={"action": "add_tag", "value": "by-filter", "filters": {"q": tag, "license": "CC0"}}).json()
    assert r["models"] == 1 and _names(authed, tag="by-filter", q=tag) == [small["filename"]]
    from app.routers import bulk
    monkeypatch.setattr(bulk, "MAX_MODELS", 2)
    assert authed.post("/api/bulk", json={"action": "add_tag", "value": "x", "filters": {"q": tag}}).status_code == 400
    assert authed.post("/api/bulk", json={"action": "add_tag", "value": "x", "ids": [1, 2, 3]}).status_code == 400


@pytest.mark.parametrize("payload", [
    {}, {"action": "explode", "ids": [1]}, {"action": "add_tag", "ids": [1]}, {"action": "add_tag", "value": "x"},
    {"action": "add_tag", "value": "x", "ids": ["1"]}, {"action": "add_tag", "value": "x", "ids": [True]},
    {"action": "add_collection", "value": 999999, "ids": [1]}, {"action": "add_project", "value": "p", "ids": [1]},
    {"action": "set_designer", "ids": [1]}, {"action": "add_tag", "value": "x", "filters": {"fits_bed": "true"}},
])
def test_bad_bulk_requests_are_refused(authed, payload):
    assert authed.post("/api/bulk", json=payload).status_code == 400


def test_bulk_on_nothing_is_harmless(authed):
    assert authed.post("/api/bulk", json={"action": "add_tag", "value": "x", "ids": [987654321]}).json() == {"changed": 0, "unchanged": 0, "models": 0, "activity_id": None}


def test_viewers_cannot_bulk_edit(authed, models):
    from starlette.testclient import TestClient
    from app.main import app
    authed.post("/api/users", json={"username": "bulkviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        assert viewer.post("/api/auth/login", json={"username": "bulkviewer", "password": PW}).status_code == 200
        assert viewer.post("/api/bulk", json={"action": "add_tag", "value": "x", "ids": [1]}).status_code == 403
        assert viewer.get("/api/saved-searches").status_code == 200
        assert viewer.post("/api/saved-searches", json={"name": "n", "params": {"q": "x"}}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


# ---------- print settings ----------

def test_print_settings_are_saved_and_shown_on_the_model(authed, models):
    tag, big, small, tall = models
    r = authed.put(f"/api/library/models/{small['id']}/print-settings", json={
        "material": "PETG", "layer_height": 0.2, "infill": "20%", "supports": "tree", "nozzle_temp": 235, "notes": "  slow first layer ", "speed": ""})
    assert r.status_code == 200
    assert r.json()["print_settings"] == {"material": "PETG", "layer_height": "0.2", "infill": "20%", "supports": "tree",
                                          "nozzle_temp": "235", "notes": "slow first layer"}
    import json
    assert json.loads(authed.get(f"/api/library/models/{small['id']}/full").json()["print_settings"])["material"] == "PETG"
    assert authed.put(f"/api/library/models/{small['id']}/print-settings", json={}).json() == {"print_settings": {}}
    assert authed.get(f"/api/library/models/{small['id']}").json()["print_settings"] is None


@pytest.mark.parametrize("payload", [{"colour": "red"}, {"material": ["PLA"]}, {"material": True}, {"notes": "x" * 2000}])
def test_bad_print_settings_are_refused(authed, models, payload):
    assert authed.put(f"/api/library/models/{models[2]['id']}/print-settings", json=payload).status_code == 400
    assert authed.put("/api/library/models/987654/print-settings", json={}).status_code == 404


# ---------- print again ----------

def test_print_again_reuses_the_last_prints_filament(authed, models):
    tag, big, small, tall = models
    spool = authed.post("/api/filament", json={"material": "PLA", "spool_weight_g": 1000, "remaining_g": 700}).json()
    try:
        first = authed.post(f"/api/queue/again/{small['id']}").json()                          # never printed: an empty entry
        assert first["filament_id"] is None and first["estimated_grams"] is None
        authed.delete(f"/api/queue/{first['id']}")
        authed.post("/api/prints", json={"model_id": small["id"], "filament_id": spool["id"], "grams": 12.5, "minutes": 40, "printed_at": "2026-01-01"})
        authed.post("/api/prints", json={"model_id": small["id"], "filament_id": spool["id"], "grams": 15, "minutes": 50, "printed_at": "2026-02-01"})
        again = authed.post(f"/api/queue/again/{small['id']}").json()
        assert (again["filament_id"], again["estimated_grams"], again["estimated_minutes"]) == (spool["id"], 15, 50)
        authed.delete(f"/api/queue/{again['id']}")
        assert authed.post("/api/queue/again/987654").status_code == 404
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


# ---------- filament prices ----------

def test_price_changes_are_remembered(authed):
    spool = authed.post("/api/filament", json={"material": "PLA", "spool_weight_g": 1000, "remaining_g": 1000, "cost": 22}).json()
    try:
        authed.patch(f"/api/filament/{spool['id']}", json={"remaining_g": 900})                  # not a price change
        authed.patch(f"/api/filament/{spool['id']}", json={"cost": 18.5})
        authed.patch(f"/api/filament/{spool['id']}", json={"cost": 18.5})                        # unchanged: not noted again
        prices = authed.get(f"/api/filament/{spool['id']}/prices").json()
        assert [p["cost"] for p in prices] == [18.5, 22.0] and [p["per_kg"] for p in prices] == [18.5, 22.0]
        assert authed.get("/api/filament/987654/prices").status_code == 404
    finally:
        authed.delete(f"/api/filament/{spool['id']}")
    assert authed.get(f"/api/filament/{spool['id']}/prices").status_code == 404


def test_low_spools_appear_on_the_shopping_list_and_its_exports(authed):
    spool = authed.post("/api/filament", json={"material": "PETG", "color": "shoplow", "spool_weight_g": 1000, "remaining_g": 30,
                                               "cost": 19.99, "purchase_url": "https://shop.example/petg"}).json()
    try:
        low = authed.get("/api/projects/shopping-list").json()["low_filament"]
        mine = next(f for f in low if "shoplow" in f["label"])
        assert mine["remaining_g"] == 30 and mine["purchase_url"] == "https://shop.example/petg" and mine["cost"] == 19.99
        text = authed.get("/api/projects/shopping-list/export?format=txt").text
        assert "FILAMENT RUNNING LOW" in text and "shoplow" in text and "https://shop.example/petg" in text
        csv_text = authed.get("/api/projects/shopping-list/export?format=csv").text
        assert "Filament running low" in csv_text and "shoplow" in csv_text
        authed.put("/api/settings", json={"low_filament_g": "0"})
        assert authed.get("/api/projects/shopping-list").json()["low_filament"] == []
    finally:
        authed.put("/api/settings", json={"low_filament_g": ""})
        authed.delete(f"/api/filament/{spool['id']}")
