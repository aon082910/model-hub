"""The cost calculator, orders for other people, and choosing a printer for a waiting print."""
import csv
import io
import uuid
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

PW = "a long enough password"


def _stl():
    n = 30 + uuid.uuid4().int % 250
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 3}\n endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def model(authed):
    m = authed.post("/api/library/import", files={"file": (f"co_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")}).json()
    yield m
    authed.delete(f"/api/library/models/{m['id']}")


@pytest.fixture(autouse=True)
def clean(authed):
    keys = ("cost_kwh_price", "cost_printer_watts", "cost_machine_per_hour", "cost_failure_pct", "cost_margin_pct", "cost_default_per_kg")
    def wipe():
        authed.put("/api/settings", json={k: "" for k in keys})
        for q in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{q['id']}")
        for o in authed.get("/api/orders").json()["orders"]:
            authed.delete(f"/api/orders/{o['id']}")
        for p in authed.get("/api/printers").json()["printers"]:
            authed.delete(f"/api/printers/{p['id']}")
    wipe()
    yield
    wipe()


def _spool(c, cost=20, grams=1000, material="PLA", color="cc"):
    return c.post("/api/filament", json={"material": material, "color": f"{color}-{uuid.uuid4().hex[:4]}", "spool_weight_g": grams, "remaining_g": grams, "cost": cost}).json()


# ---------- cost calculator ----------

def test_filament_electricity_machine_failures_and_margin_add_up(authed):
    spool = _spool(authed, cost=25, grams=1000)                                            # 2.5 cents a gram
    try:
        authed.put("/api/settings", json={"cost_kwh_price": "0.20", "cost_printer_watts": "200", "cost_machine_per_hour": "1", "cost_failure_pct": "10", "cost_margin_pct": "50"})
        q = authed.get("/api/costs/quote", params={"grams": 100, "minutes": 120, "filament_id": spool["id"]}).json()
        # filament 2.50, electricity 2 h * 0.2 kW * 0.20 = 0.08, machine 2.00 -> 4.58; +10% = 5.038; +50% = 7.557
        assert q["unit"] == {"filament": 2.5, "electricity": 0.08, "machine": 2.0, "failures": 0.46, "cost": 5.04, "price": 7.56}
        assert q["failure_from"] == "your setting" and q["filament_from"] == "the spool's price" and q["missing"] == []
        many = authed.get("/api/costs/quote", params={"grams": 100, "minutes": 120, "filament_id": spool["id"], "quantity": 4}).json()
        assert many["total"]["price"] == round(7.557 * 4, 2) and many["quantity"] == 4
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_missing_prices_are_said_not_guessed(authed):
    q = authed.get("/api/costs/quote", params={"grams": 50, "minutes": 60}).json()
    assert q["unit"]["filament"] is None and q["unit"]["electricity"] is None and "a price for the filament" in q["missing"] and "the price per kWh" in q["missing"]
    assert q["filament_from"] == "no price known"
    authed.put("/api/settings", json={"cost_default_per_kg": "20"})
    assert authed.get("/api/costs/quote", params={"grams": 50, "minutes": 60}).json()["unit"]["filament"] == 1.0


def test_a_spool_without_a_price_uses_the_average_of_its_material(authed):
    priced_a, priced_b = _spool(authed, cost=20, material="PETG"), _spool(authed, cost=30, material="PETG")
    bare = authed.post("/api/filament", json={"material": "PETG", "color": "bare", "spool_weight_g": 1000, "remaining_g": 1000}).json()
    try:
        q = authed.get("/api/costs/quote", params={"grams": 100, "minutes": 60, "filament_id": bare["id"]}).json()
        assert q["unit"]["filament"] == 2.5 and q["filament_from"] == "the average PETG spool"
    finally:
        for s in (priced_a, priced_b, bare):
            authed.delete(f"/api/filament/{s['id']}")


def test_the_failure_allowance_comes_from_your_own_record_once_there_is_one(authed, model):
    assert authed.get("/api/costs/quote", params={"grams": 10, "minutes": 10}).json()["failure_from"].startswith("a default")
    for i in range(10):
        authed.post("/api/prints", json={"model_id": model["id"], "deduct": False, **({"outcome": "failed"} if i < 4 else {})})
    q = authed.get("/api/costs/quote", params={"grams": 10, "minutes": 10}).json()
    assert q["failure_pct"] >= 4 and "your own record" in q["failure_from"]


def test_a_model_supplies_its_own_grams_and_minutes(authed, model):
    gcode = b"G28\n; filament used [g] = 42.0\n; estimated printing time (normal mode) = 1h 30m 0s\n"
    authed.post("/api/print-files", data={"model_id": str(model["id"])}, files={"file": ("p.gcode", gcode, "application/octet-stream")})
    q = authed.get("/api/costs/quote", params={"model_id": model["id"]}).json()
    assert q["grams"] == 42.0 and q["minutes"] == 90 and q["from_model"]["grams"] == 42.0
    assert authed.get("/api/costs/quote", params={"model_id": model["id"], "grams": 10}).json()["grams"] == 10.0          # what you give wins
    assert authed.get("/api/costs/quote", params={"model_id": 987654}).status_code == 404


@pytest.mark.parametrize("params", [{}, {"grams": 5}, {"grams": -1, "minutes": 5}, {"grams": 5, "minutes": 5, "quantity": 0}, {"grams": 5, "minutes": 5, "filament_id": 987654}])
def test_bad_quotes_are_refused(authed, params):
    assert authed.get("/api/costs/quote", params=params).status_code in (400, 404)


# ---------- orders ----------

def _order(c, model, **extra):
    r = c.post("/api/orders", json={"customer": "Dana", "items": [{"model_id": model["id"], "quantity": 3, "unit_price": 12.5}], **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_an_order_has_items_prices_and_a_profit(authed, model):
    spool = _spool(authed, cost=20)
    try:
        authed.post("/api/prints", json={"model_id": model["id"], "minutes": 60, "grams": 50, "filament_id": spool["id"], "deduct": False})
        o = _order(authed, model, due_date="2099-01-01", contact="dana@example.com")
        assert o["status"] == "quote" and o["units"] == 3 and o["price"] == 37.5 and o["items"][0]["line_price"] == 37.5 and o["overdue"] is False
        assert o["items"][0]["line_cost"] is not None and o["profit"] == round(o["price"] - o["cost"], 2)
        priced = authed.post(f"/api/orders/{o['id']}/items", json={"model_id": model["id"], "quantity": 2}).json()
        assert priced["items"][1]["unit_price"] is None and priced["items"][1]["price_used"] == priced["items"][1]["suggested_price"]        # the calculator's price
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_bad_orders_are_refused(authed, model):
    for body in ({}, {"customer": ""}, {"customer": "x", "due_date": "soon"}, {"customer": "x", "due_date": "2030-02-30"}, {"customer": "x", "items": [{"model_id": 987654}]},
                 {"customer": "x", "items": [{"model_id": model["id"], "quantity": 0}]}, {"customer": "x", "items": [{"model_id": model["id"], "unit_price": -1}]},
                 {"customer": "x", "items": "all of them"}, {"customer": 5}):
        assert authed.post("/api/orders", json=body).status_code == 400, body
    o = _order(authed, model)
    assert authed.patch(f"/api/orders/{o['id']}", json={"status": "shipped"}).status_code == 400
    assert authed.patch(f"/api/orders/{o['id']}", json={"paid": "yes"}).status_code == 400
    assert authed.get("/api/orders/987654").status_code == 404 and authed.get("/api/orders", params={"status": "nope"}).status_code == 400


def test_queueing_an_order_adds_one_entry_per_unit_and_only_what_is_missing(authed, model):
    o = _order(authed, model, due_date="2099-05-01")
    r = authed.post(f"/api/orders/{o['id']}/queue", json={}).json()
    assert r["queued"] == 3 and r["order"]["status"] == "accepted"
    entries = [q for q in authed.get("/api/queue").json() if q["order_id"] == o["id"]]
    assert len(entries) == 3 and all(q["planned_date"] == "2099-05-01" and q["notes"].startswith("Order") for q in entries)
    assert authed.post(f"/api/orders/{o['id']}/queue", json={}).json()["queued"] == 0                      # nothing twice
    authed.patch(f"/api/queue/{entries[0]['id']}", json={"status": "failed"})                              # a failed one is made again
    assert authed.post(f"/api/orders/{o['id']}/queue", json={}).json()["queued"] == 1
    authed.post(f"/api/orders/{o['id']}/items", json={"model_id": model["id"], "quantity": 2})
    assert authed.post(f"/api/orders/{o['id']}/queue", json={}).json()["queued"] == 2
    assert authed.post(f"/api/orders/{o['id']}/queue", json={"printer_id": 987654}).status_code == 400


def test_progress_follows_the_prints_and_suggests_the_next_status(authed, model):
    o = _order(authed, model)
    authed.post(f"/api/orders/{o['id']}/queue", json={})
    entries = [q for q in authed.get("/api/queue").json() if q["order_id"] == o["id"]]
    authed.patch(f"/api/queue/{entries[0]['id']}", json={"status": "done"})
    d = authed.get(f"/api/orders/{o['id']}").json()
    assert d["units_done"] == 1 and d["suggested_status"] == "printing" and d["items"][0]["units_done"] == 1 and d["items"][0]["units_queued"] == 3
    for e in entries[1:]:
        authed.patch(f"/api/queue/{e['id']}", json={"status": "done"})
    assert authed.get(f"/api/orders/{o['id']}").json()["suggested_status"] == "ready"
    done = authed.patch(f"/api/orders/{o['id']}", json={"status": "delivered", "paid": True}).json()
    assert done["status"] == "delivered" and done["paid"] is True


def test_an_order_past_its_date_is_marked_overdue(authed, model):
    past = (date.today() - timedelta(days=3)).isoformat()
    o = authed.post("/api/orders", json={"customer": "Late", "due_date": past, "items": []}).json()
    assert o["overdue"] is True
    assert authed.patch(f"/api/orders/{o['id']}", json={"status": "delivered"}).json()["overdue"] is False


def test_removing_an_order_or_an_item_leaves_the_prints_in_the_queue(authed, model):
    o = _order(authed, model)
    authed.post(f"/api/orders/{o['id']}/queue", json={})
    item_id = authed.get(f"/api/orders/{o['id']}").json()["items"][0]["id"]
    assert authed.delete(f"/api/orders/{o['id']}/items/{item_id}").json()["items"] == []
    assert all(q["order_id"] is None for q in authed.get("/api/queue").json())
    assert authed.delete(f"/api/orders/{o['id']}").status_code == 200 and authed.get(f"/api/orders/{o['id']}").status_code == 404
    assert len(authed.get("/api/queue").json()) == 3


def test_the_csv_is_safe_for_spreadsheets(authed, model):
    o = authed.post("/api/orders", json={"customer": '=HYPERLINK("http://evil")', "notes": "+cmd", "items": [{"model_id": model["id"], "quantity": 2, "unit_price": 5}]}).json()
    r = authed.get("/api/orders/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    mine = next(x for x in rows if x["order"] == str(o["id"]))
    assert mine["customer"].startswith("'=") and mine["notes"].startswith("'+") and mine["quantity"] == "2" and mine["line price"] == "10.0"


def test_viewers_can_read_orders_but_not_change_them(authed, model):
    from app.main import app
    _order(authed, model)
    authed.post("/api/users", json={"username": "ordviewer", "password": PW, "role": "viewer"})
    try:
        viewer = TestClient(app)
        viewer.post("/api/auth/login", json={"username": "ordviewer", "password": PW})
        assert viewer.get("/api/orders").status_code == 200
        assert viewer.post("/api/orders", json={"customer": "x"}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


# ---------- choosing a printer ----------

def _printer(c, name, **extra):
    return c.post("/api/printers", json={"name": name, "kind": "moonraker", "url": f"http://klipper.local:{7000 + uuid.uuid4().int % 999}", **extra}).json()


def test_a_waiting_print_is_suggested_the_printer_that_fits_and_has_the_spool(authed, model):
    small = _printer(authed, "Small", bed_x=10, bed_y=10, bed_z=10, slot_count=2)
    big = _printer(authed, "Big", bed_x=400, bed_y=400, bed_z=400, slot_count=2)
    idle = _printer(authed, "Idle", slot_count=2)
    pla = _spool(authed, material="PLA", color="route-pla")
    petg = _spool(authed, material="PETG", color="route-petg")
    try:
        authed.put(f"/api/slots/{big['id']}/2", json={"filament_id": pla["id"]})
        authed.put(f"/api/slots/{idle['id']}/1", json={"filament_id": petg["id"]})
        item = authed.post("/api/queue", json={"model_id": model["id"], "filament_id": pla["id"], "estimated_minutes": 60}).json()
        s = authed.get("/api/queue/suggestions").json()[str(item["id"])]
        assert [c["name"] for c in s][0] == "Big" and s[0]["slot"] == 2 and "has that spool in slot 2" in s[0]["reasons"]
        assert "Small" not in [c["name"] for c in s]                                                          # too small for the model
        idle_row = next(c for c in s if c["name"] == "Idle")
        assert idle_row["slot"] is None and any("no PLA loaded" in r for r in idle_row["reasons"])
    finally:
        for sp in (pla, petg):
            authed.delete(f"/api/filament/{sp['id']}")


def test_a_busy_printer_ranks_below_a_free_one(authed, model):
    busy, free = _printer(authed, "Busy"), _printer(authed, "Free")
    for _ in range(3):
        authed.post("/api/queue", json={"model_id": model["id"], "printer_id": busy["id"], "estimated_minutes": 240})
    item = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 30}).json()
    s = authed.get("/api/queue/suggestions").json()[str(item["id"])]
    assert [c["name"] for c in s] == ["Free", "Busy"] and "nothing waiting" in s[0]["reasons"]
    assert str(next(q["id"] for q in authed.get("/api/queue").json() if q["printer_id"] == busy["id"])) not in authed.get("/api/queue/suggestions").json()   # already has one


def test_assigning_all_suggestions_can_be_previewed_and_undone(authed, model):
    p = _printer(authed, "Only")
    a = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 30}).json()
    b = authed.post("/api/queue", json={"model_id": model["id"], "estimated_minutes": 30}).json()
    keep = authed.post("/api/queue", json={"model_id": model["id"], "printer_id": p["id"]}).json()
    dry = authed.post("/api/queue/auto-assign", json={"dry_run": True}).json()
    assert dry["dry_run"] is True and dry["activity_id"] is None and {x["id"] for x in dry["assigned"]} == {a["id"], b["id"]}
    assert all(q["printer_id"] in (None, p["id"]) for q in authed.get("/api/queue").json()) and next(q for q in authed.get("/api/queue").json() if q["id"] == a["id"])["printer_id"] is None
    real = authed.post("/api/queue/auto-assign", json={}).json()
    assert {q["id"]: q["printer_id"] for q in authed.get("/api/queue").json()} == {a["id"]: p["id"], b["id"]: p["id"], keep["id"]: p["id"]}
    authed.patch(f"/api/queue/{b['id']}", json={"status": "printing"})                                        # started since: stays
    undone = authed.post(f"/api/activity/{real['activity_id']}/undo")
    assert undone.status_code == 200 and undone.json()["restored"] == 1
    rows = {q["id"]: q["printer_id"] for q in authed.get("/api/queue").json()}
    assert rows[a["id"]] is None and rows[b["id"]] == p["id"]
    assert authed.post("/api/queue/auto-assign", json={"item_ids": "all"}).status_code == 400
