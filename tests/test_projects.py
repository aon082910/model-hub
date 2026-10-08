"""Projects with a parts list (electronics / parts / supplies) and the
"projects needing parts" views."""


def _ensure_authenticated(client):
    from conftest import ensure_authenticated
    ensure_authenticated(client)


def _project(client, name, **extra):
    r = client.post("/api/projects", json={"name": name, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _part(client, project_id, name, **extra):
    r = client.post(f"/api/projects/{project_id}/parts", json={"name": name, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_projects_require_auth(client):
    client.post("/api/auth/logout")
    assert client.get("/api/projects").status_code == 401
    _ensure_authenticated(client)


def test_create_project_validates(client):
    _ensure_authenticated(client)
    assert client.post("/api/projects", json={"name": "   "}).status_code == 400
    assert client.post("/api/projects", json={"name": "x", "status": "bogus"}).status_code == 400
    p = _project(client, "  Weather station ")
    assert p["name"] == "Weather station"
    assert p["status"] == "planning"
    assert p["parts"] == [] and p["parts_missing"] == 0


def test_part_needed_math_and_validation(client):
    _ensure_authenticated(client)
    p = _project(client, "Math project")
    pid = p["id"]

    part = _part(client, pid, "ESP32", category="electronics", quantity=3, quantity_owned=1, unit_cost=8.5)
    assert part["quantity_needed"] == 2
    assert part["cost_needed"] == 17.0

    # owning more than needed never produces a negative need
    surplus = _part(client, pid, "M3 screw", category="parts", quantity=4, quantity_owned=10)
    assert surplus["quantity_needed"] == 0
    assert surplus["cost_needed"] is None

    bad = [
        {"name": ""},
        {"name": "x", "category": "weapons"},
        {"name": "x", "quantity": 0},
        {"name": "x", "quantity": "many"},
        {"name": "x", "quantity_owned": -1},
        {"name": "x", "unit_cost": -2},
        {"name": "x", "unit_cost": "free"},
    ]
    for payload in bad:
        assert client.post(f"/api/projects/{pid}/parts", json=payload).status_code == 400, payload
    assert client.post("/api/projects/999999/parts", json={"name": "x"}).status_code == 404

    got = client.get(f"/api/projects/{pid}").json()
    assert got["parts_total"] == 2
    assert got["parts_missing"] == 1
    assert got["cost_needed"] == 17.0


def test_update_part_marks_owned(client):
    _ensure_authenticated(client)
    pid = _project(client, "Update project")["id"]
    part = _part(client, pid, "Resistor 10k", category="electronics", quantity=5, unit_cost=0.1)
    assert part["quantity_needed"] == 5

    r = client.patch(f"/api/projects/{pid}/parts/{part['id']}", json={"quantity_owned": 5})
    assert r.status_code == 200
    assert r.json()["quantity_needed"] == 0
    assert client.get(f"/api/projects/{pid}").json()["parts_missing"] == 0

    # a part can't be edited through a different project's URL
    other = _project(client, "Other project")["id"]
    assert client.patch(f"/api/projects/{other}/parts/{part['id']}", json={"quantity": 2}).status_code == 404
    assert client.delete(f"/api/projects/{other}/parts/{part['id']}").status_code == 404


def test_projects_list_only_needing_parts(client):
    _ensure_authenticated(client)
    needs = _project(client, "Needs parts proj")["id"]
    _part(client, needs, "OLED display", category="electronics", quantity=1)
    _part(client, needs, "Hot glue", category="supplies", quantity=2, quantity_owned=2)
    complete = _project(client, "Complete proj")["id"]
    _part(client, complete, "Jumper wires", category="supplies", quantity=1, quantity_owned=1)
    _project(client, "Empty proj")

    names = {p["name"]: p for p in client.get("/api/projects?only_needing_parts=true").json()}
    assert "Needs parts proj" in names
    assert "Complete proj" not in names and "Empty proj" not in names
    # only the missing parts are listed for a project in this view
    assert [part["name"] for part in names["Needs parts proj"]["parts"]] == ["OLED display"]

    everything = {p["name"] for p in client.get("/api/projects").json()}
    assert {"Needs parts proj", "Complete proj", "Empty proj"} <= everything


def test_shopping_list_skips_done_projects(client):
    _ensure_authenticated(client)
    active = _project(client, "Shopping active")["id"]
    _part(client, active, "Shopping servo", category="electronics", quantity=2, unit_cost=3.0)
    finished = _project(client, "Shopping finished", status="done")["id"]
    _part(client, finished, "Shopping leftover", category="parts", quantity=1)

    data = client.get("/api/projects/shopping-list").json()
    by_name = {i["name"]: i for i in data["items"]}
    assert by_name["Shopping servo"]["project_name"] == "Shopping active"
    assert by_name["Shopping servo"]["cost_needed"] == 6.0
    assert "Shopping leftover" not in by_name
    assert data["total_cost"] >= 6.0

    # finishing the project removes its parts from the list
    client.patch(f"/api/projects/{active}", json={"status": "done"})
    names = {i["name"] for i in client.get("/api/projects/shopping-list").json()["items"]}
    assert "Shopping servo" not in names


def test_link_models_to_project(client):
    _ensure_authenticated(client)
    imported = client.post("/api/library/import", files={"file": ("proj-link.stl", _stl(), "application/octet-stream")})
    assert imported.status_code == 200, imported.text
    model = imported.json()

    pid = _project(client, "Linked project")["id"]
    linked = client.post(f"/api/projects/{pid}/models/{model['id']}").json()
    assert [m["id"] for m in linked["models"]] == [model["id"]]
    # linking twice is a no-op
    assert len(client.post(f"/api/projects/{pid}/models/{model['id']}").json()["models"]) == 1
    assert client.post(f"/api/projects/{pid}/models/999999").status_code == 404

    unlinked = client.delete(f"/api/projects/{pid}/models/{model['id']}").json()
    assert unlinked["models"] == []
    assert client.delete(f"/api/projects/{pid}/models/{model['id']}").status_code == 404


def test_delete_project_removes_parts_and_links(client):
    _ensure_authenticated(client)
    pid = _project(client, "Doomed project")["id"]
    part = _part(client, pid, "Doomed part", quantity=1)

    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404

    from sqlmodel import Session
    from app.db import engine
    from app.models import ProjectPart
    with Session(engine) as session:
        assert session.get(ProjectPart, part["id"]) is None


def _stl() -> bytes:
    return b"""solid t
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 5 0 0
  vertex 0 5 0
 endloop
endfacet
endsolid t
"""


# ---------- filament per model + deduction on printed ----------

def _spool(client, remaining, material="PLA", color="Red"):
    r = client.post("/api/filament", json={
        "material": material, "brand": "ProjTest", "color": color,
        "spool_weight_g": 1000, "remaining_g": remaining,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _remaining(client, spool_id):
    return next(f for f in client.get("/api/filament").json() if f["id"] == spool_id)["remaining_g"]


def _project_with_model(client, name, filename):
    imported = client.post("/api/library/import", files={"file": (filename, _stl(), "application/octet-stream")})
    assert imported.status_code == 200, imported.text
    model = imported.json()
    pid = _project(client, name)["id"]
    assert client.post(f"/api/projects/{pid}/models/{model['id']}").status_code == 200
    return pid, model["id"]


def test_filament_lines_validate_and_total(client):
    _ensure_authenticated(client)
    pid, mid = _project_with_model(client, "Filament totals", "fil-totals.stl")
    red, blue = _spool(client, 100), _spool(client, 500, color="Blue")

    base = f"/api/projects/{pid}/models/{mid}/filament"
    assert client.post(base, json={"grams": 5}).status_code == 400
    assert client.post(base, json={"filament_id": 999999, "grams": 5}).status_code == 404
    assert client.post(base, json={"filament_id": red["id"], "grams": -1}).status_code == 400
    assert client.post(base, json={"filament_id": red["id"], "grams": "lots"}).status_code == 400
    assert client.post(f"/api/projects/{pid}/models/999999/filament",
                       json={"filament_id": red["id"], "grams": 1}).status_code == 404

    client.post(base, json={"filament_id": red["id"], "grams": 120})
    data = client.post(base, json={"filament_id": blue["id"], "grams": 30}).json()
    assert data["filament_grams"] == 150
    model = data["models"][0]
    assert model["filament_grams"] == 150 and len(model["filament"]) == 2
    totals = {t["filament_id"]: t for t in data["filament_totals"]}
    assert totals[red["id"]]["short"] is True    # needs 120g, spool has 100g
    assert totals[blue["id"]]["short"] is False

    line = model["filament"][0]
    patched = client.patch(f"/api/projects/{pid}/filament/{line['id']}", json={"grams": 80}).json()
    assert patched["filament_grams"] == 110
    assert client.delete(f"/api/projects/{pid}/filament/{line['id']}").json()["filament_grams"] == 30
    other = _project(client, "Other filament proj")["id"]
    assert client.patch(f"/api/projects/{other}/filament/{line['id']}", json={"grams": 1}).status_code == 404


def test_printed_deducts_once_and_revert_restores(client):
    _ensure_authenticated(client)
    pid, mid = _project_with_model(client, "Deduct project", "fil-deduct.stl")
    red, blue = _spool(client, 200), _spool(client, 50, color="Blue")
    base = f"/api/projects/{pid}/models/{mid}/filament"
    client.post(base, json={"filament_id": red["id"], "grams": 60})
    client.post(base, json={"filament_id": red["id"], "grams": 15})
    client.post(base, json={"filament_id": blue["id"], "grams": 80})   # more than the spool holds

    # nothing is taken while planning/building
    client.patch(f"/api/projects/{pid}", json={"status": "building"})
    assert _remaining(client, red["id"]) == 200

    printed = client.patch(f"/api/projects/{pid}", json={"status": "printed"}).json()
    assert printed["filament_deducted"] is True
    assert _remaining(client, red["id"]) == 125
    assert _remaining(client, blue["id"]) == 0     # clamped at what was available

    # printed -> done and repeated saves never deduct a second time
    client.patch(f"/api/projects/{pid}", json={"status": "done"})
    client.patch(f"/api/projects/{pid}", json={"status": "done"})
    assert _remaining(client, red["id"]) == 125

    # filament and models are locked once spent
    assert client.post(base, json={"filament_id": red["id"], "grams": 1}).status_code == 409
    assert client.delete(f"/api/projects/{pid}/models/{mid}").status_code == 409

    # moving back gives back exactly what was taken (50g for blue, not 80g)
    back = client.patch(f"/api/projects/{pid}", json={"status": "building"}).json()
    assert back["filament_deducted"] is False
    assert _remaining(client, red["id"]) == 200
    assert _remaining(client, blue["id"]) == 50
    assert client.post(base, json={"filament_id": red["id"], "grams": 1}).status_code == 200


def test_done_directly_from_planning_also_deducts(client):
    _ensure_authenticated(client)
    pid, mid = _project_with_model(client, "Straight to done", "fil-done.stl")
    spool = _spool(client, 100)
    client.post(f"/api/projects/{pid}/models/{mid}/filament", json={"filament_id": spool["id"], "grams": 25})
    client.patch(f"/api/projects/{pid}", json={"status": "done"})
    assert _remaining(client, spool["id"]) == 75


def test_removing_model_drops_its_filament_lines(client):
    _ensure_authenticated(client)
    pid, mid = _project_with_model(client, "Unlink filament", "fil-unlink.stl")
    spool = _spool(client, 100)
    client.post(f"/api/projects/{pid}/models/{mid}/filament", json={"filament_id": spool["id"], "grams": 10})
    data = client.delete(f"/api/projects/{pid}/models/{mid}").json()
    assert data["filament_grams"] == 0 and data["models"] == []


def test_deleted_spool_does_not_break_project(client):
    _ensure_authenticated(client)
    pid, mid = _project_with_model(client, "Deleted spool", "fil-gone.stl")
    spool = _spool(client, 100)
    client.post(f"/api/projects/{pid}/models/{mid}/filament", json={"filament_id": spool["id"], "grams": 10})
    client.delete(f"/api/filament/{spool['id']}")
    data = client.get(f"/api/projects/{pid}").json()
    assert data["models"][0]["filament"][0]["filament_label"] == "(deleted spool)"
    assert client.patch(f"/api/projects/{pid}", json={"status": "printed"}).status_code == 200
    assert client.patch(f"/api/projects/{pid}", json={"status": "planning"}).status_code == 200


# ---------- bulk add (a listing's parts list) ----------

def test_bulk_add_parts(client):
    _ensure_authenticated(client)
    pid = _project(client, "Bulk project")["id"]
    rows = [
        {"name": "Bulk servo", "category": "electronics", "quantity": 4, "unit_cost": 5.49, "purchase_url": "https://example.com/s"},
        {"name": "Bulk screw", "category": "parts", "quantity": 2, "notes": "from listing"},
    ]
    r = client.post(f"/api/projects/{pid}/parts/bulk", json={"parts": rows})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 2
    got = client.get(f"/api/projects/{pid}").json()
    assert got["parts_total"] == 2 and got["parts_missing"] == 2 and got["cost_needed"] == 21.96


def test_bulk_add_is_all_or_nothing(client):
    _ensure_authenticated(client)
    pid = _project(client, "Bulk bad row")["id"]
    rows = [{"name": "Fine part", "category": "parts"}, {"name": "Bad part", "category": "weapons"}]
    r = client.post(f"/api/projects/{pid}/parts/bulk", json={"parts": rows})
    assert r.status_code == 400 and "part 2" in r.json()["detail"]
    assert client.get(f"/api/projects/{pid}").json()["parts_total"] == 0     # the good row was not added either

    for payload in ({}, {"parts": []}, {"parts": "x"}, {"parts": ["not an object"]}):
        assert client.post(f"/api/projects/{pid}/parts/bulk", json=payload).status_code == 400, payload
    too_many = [{"name": f"p{i}", "category": "parts"} for i in range(201)]
    assert client.post(f"/api/projects/{pid}/parts/bulk", json={"parts": too_many}).status_code == 400
    assert client.post("/api/projects/999999/parts/bulk", json={"parts": rows[:1]}).status_code == 404
