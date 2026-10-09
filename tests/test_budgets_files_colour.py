"""Budgets with reservations and a ledger, sliced files made for one printer, an exact-colour switch, and orders that move on by themselves."""
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session

from app import budgets
from app.db import engine
from app.models import CostCentre, LedgerEntry, QueueItem
from test_print_farm import farm, _keep, _queue, GCODE  # noqa: F401  (the fixture and helpers)


@pytest.fixture()
def money(authed):
    """Prices that make the sums easy: 20 per kg, no electricity, no margin, no failure allowance."""
    authed.put("/api/settings", json={"cost_default_per_kg": "20", "cost_failure_pct": "0", "cost_margin_pct": "0", "cost_kwh_price": "", "cost_machine_per_hour": "",
                                      "strict_colour": ""})
    yield
    for c in authed.get("/api/budgets").json()["budgets"]:
        authed.delete(f"/api/budgets/{c['id']}")
    authed.put("/api/settings", json={"cost_default_per_kg": "", "cost_failure_pct": "", "cost_margin_pct": "", "strict_colour": ""})


def _centre(c, **extra):
    r = c.post("/api/budgets", json={"name": "Club", "budget": 10, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _find(c, centre_id):
    return next(b for b in c.get("/api/budgets").json()["budgets"] if b["id"] == centre_id)


# ---------------------------------------------------------------- budgets
def test_a_budget_is_checked_saved_renamed_and_removed(authed, money):
    for bad in ({"name": "", "budget": 5}, {"name": "x", "budget": -1}, {"name": "x", "budget": "lots"}, {"name": "x", "budget": 5, "period": "week"},
                {"name": "x", "budget": 5, "hard_stop": "yes"}):
        assert authed.post("/api/budgets", json=bad).status_code == 400, bad
    c = _centre(authed, period="month")
    assert c["budget"] == 10 and c["period"] == "month" and c["hard_stop"] is False and c["spent"] == 0 and c["reserved"] == 0 and c["available"] == 10
    assert authed.post("/api/budgets", json={"name": "club", "budget": 1}).status_code == 400                          # names are not case-sensitive
    changed = authed.patch(f"/api/budgets/{c['id']}", json={"name": "Robotics", "budget": 25.5, "hard_stop": True, "period": "total"}).json()
    assert (changed["name"], changed["budget"], changed["hard_stop"], changed["available"]) == ("Robotics", 25.5, True, 25.5)
    assert authed.patch(f"/api/budgets/{c['id']}", json={"budget": "x"}).status_code == 400
    assert authed.delete(f"/api/budgets/{c['id']}").json() == {"status": "deleted"}
    assert authed.patch(f"/api/budgets/{c['id']}", json={"budget": 1}).status_code == 404


def test_queued_prints_reserve_money_and_unpriced_ones_are_counted(authed, farm, money):
    fake, a, b, m, _ = farm
    c = _centre(authed)
    _queue(authed, farm, cost_centre_id=c["id"], estimated_grams=100, estimated_minutes=60)                           # 100 g at 20/kg = 2.00
    _queue(authed, farm, cost_centre_id=c["id"], estimated_grams=50, estimated_minutes=30)                            # 1.00
    _queue(authed, farm, cost_centre_id=c["id"])                                                                        # nothing known: reserves nothing
    info = _find(authed, c["id"])
    assert info["reserved"] == 3.0 and info["available"] == 7.0 and info["unpriced"] == 1 and info["spent"] == 0
    assert authed.post("/api/queue", json={"model_id": m["id"], "cost_centre_id": 987654}).status_code == 400


def test_a_budget_set_to_stop_refuses_what_would_go_over(authed, farm, money):
    fake, a, b, m, _ = farm
    c = _centre(authed, budget=3, hard_stop=True)
    first = _queue(authed, farm, cost_centre_id=c["id"], estimated_grams=100, estimated_minutes=60)                   # 2.00 of 3.00
    before = len(authed.get("/api/queue").json())
    r = authed.post("/api/queue", json={"model_id": m["id"], "cost_centre_id": c["id"], "estimated_grams": 100, "estimated_minutes": 60})
    assert r.status_code == 409 and r.json()["detail"].startswith("Budget: ") and "over by 1.00" in r.json()["detail"]
    assert len(authed.get("/api/queue").json()) == before                                                               # nothing was saved
    loose = _queue(authed, farm, estimated_grams=100, estimated_minutes=60)
    over = authed.patch(f"/api/queue/{loose['id']}", json={"cost_centre_id": c["id"]})
    assert over.status_code == 409
    assert authed.get(f"/api/queue").json() and next(q for q in authed.get("/api/queue").json() if q["id"] == loose["id"])["cost_centre_id"] is None
    authed.patch(f"/api/budgets/{c['id']}", json={"budget": 10})
    assert authed.patch(f"/api/queue/{loose['id']}", json={"cost_centre_id": c["id"]}).status_code == 200
    authed.patch(f"/api/budgets/{c['id']}", json={"hard_stop": False, "budget": 1})
    assert _queue(authed, farm, cost_centre_id=c["id"], estimated_grams=100, estimated_minutes=60)                      # not set to stop: only a warning figure
    assert _find(authed, c["id"])["available"] < 0


def test_a_finished_print_is_charged_once_and_the_reservation_is_released(authed, farm, money):
    fake, a, b, m, _ = farm
    c = _centre(authed)
    item = _queue(authed, farm, cost_centre_id=c["id"], estimated_grams=100, estimated_minutes=60)
    authed.patch(f"/api/queue/{item['id']}", json={"status": "done"})
    info = _find(authed, c["id"])
    assert info["spent"] == 2.0 and info["reserved"] == 0 and info["available"] == 8.0
    authed.patch(f"/api/queue/{item['id']}", json={"status": "printing"})
    authed.patch(f"/api/queue/{item['id']}", json={"status": "done"})                                                   # done twice is charged once
    assert _find(authed, c["id"])["spent"] == 2.0
    ledger = authed.get(f"/api/budgets/{c['id']}/ledger").json()
    assert [(e["kind"], e["amount"]) for e in ledger["entries"]] == [("print", 2.0)] and ledger["budget"]["spent"] == 2.0


def test_a_failed_print_is_charged_only_for_the_filament_it_used(authed, farm, money):
    from app.routers.queue import fail_item
    fake, a, b, m, _ = farm
    c = _centre(authed)
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "red", "spool_weight_g": 1000, "remaining_g": 900}).json()
    try:
        item = _queue(authed, farm, cost_centre_id=c["id"], filament_id=spool["id"], estimated_grams=100, estimated_minutes=600)
        with Session(engine) as s:
            fail_item(s, s.get(QueueItem, item["id"]), progress=50)
            s.commit()
        entries = authed.get(f"/api/budgets/{c['id']}/ledger").json()["entries"]
        assert [(e["kind"], e["amount"]) for e in entries] == [("failed", 1.0)]                                         # 50 g at 20/kg, no machine time or allowance
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_a_monthly_budget_forgets_last_months_spending(authed, money):
    c = _centre(authed, period="month")
    t = _centre_row = c["id"]
    with Session(engine) as s:
        s.add(LedgerEntry(cost_centre_id=t, amount=4.0, kind="manual", created_at=datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)))
        s.add(LedgerEntry(cost_centre_id=t, amount=1.5, kind="manual"))
        s.commit()
    assert _find(authed, t)["spent"] == 1.5
    authed.patch(f"/api/budgets/{t}", json={"period": "total"})
    assert _find(authed, t)["spent"] == 5.5


def test_spending_can_be_written_in_credited_and_taken_out_again(authed, money):
    c = _centre(authed)
    r = authed.post(f"/api/budgets/{c['id']}/entries", json={"amount": 4.25, "note": "  new nozzle  "}).json()
    assert r["spent"] == 4.25
    assert authed.post(f"/api/budgets/{c['id']}/entries", json={"amount": -1}).json()["spent"] == 3.25                    # a credit
    for bad in ({"amount": 0}, {"amount": "x"}, {"amount": 5, "note": 7}):
        assert authed.post(f"/api/budgets/{c['id']}/entries", json=bad).status_code == 400, bad
    entries = authed.get(f"/api/budgets/{c['id']}/ledger").json()["entries"]
    assert entries[1]["note"] == "new nozzle"
    assert authed.delete(f"/api/budgets/{c['id']}/entries/{entries[0]['id']}").status_code == 200
    assert authed.delete(f"/api/budgets/{c['id']}/entries/{entries[0]['id']}").status_code == 404
    assert _find(authed, c["id"])["spent"] == 4.25


def test_removing_a_budget_frees_its_prints_and_forgets_its_ledger(authed, farm, money):
    fake, a, b, m, _ = farm
    c = _centre(authed)
    item = _queue(authed, farm, cost_centre_id=c["id"], estimated_grams=100, estimated_minutes=60)
    authed.post(f"/api/budgets/{c['id']}/entries", json={"amount": 3})
    authed.delete(f"/api/budgets/{c['id']}")
    assert next(q for q in authed.get("/api/queue").json() if q["id"] == item["id"])["cost_centre_id"] is None
    with Session(engine) as s:
        assert s.query(LedgerEntry).filter(LedgerEntry.cost_centre_id == c["id"]).count() == 0


def test_a_budget_set_to_stop_also_stops_a_send(authed, farm, money):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    c = _centre(authed, budget=100, hard_stop=True)
    item = _queue(authed, farm, printer_id=a["id"], cost_centre_id=c["id"], estimated_grams=100, estimated_minutes=60)
    fake.moonraker_state = "standby"
    assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200
    authed.post(f"/api/budgets/{c['id']}/entries", json={"amount": 99})                                                  # spent 99 + reserved 2 > 100
    r = authed.post(f"/api/queue/{item['id']}/send", json={"start": True, "force": True})
    assert r.status_code == 409 and r.json()["detail"].startswith("Budget: ")                                           # force does not override a budget


def test_an_order_is_charged_to_its_budget_and_moves_on_by_itself(authed, farm, money):
    fake, a, b, m, _ = farm
    c = _centre(authed)
    order = authed.post("/api/orders", json={"customer": "Budget Buyer", "items": [{"model_id": m["id"], "quantity": 2}]}).json()
    try:
        assert authed.patch(f"/api/orders/{order['id']}", json={"cost_centre_id": 987654}).status_code == 400
        assert authed.patch(f"/api/orders/{order['id']}", json={"cost_centre_id": c["id"]}).json()["cost_centre_id"] == c["id"]
        assert authed.post(f"/api/orders/{order['id']}/queue", json={}).json()["queued"] == 2
        mine = [q for q in authed.get("/api/queue").json() if q["order_id"] == order["id"]]
        assert len(mine) == 2 and all(q["cost_centre_id"] == c["id"] for q in mine)
        authed.patch(f"/api/queue/{mine[0]['id']}", json={"estimated_grams": 10, "estimated_minutes": 10})
        authed.patch(f"/api/queue/{mine[0]['id']}", json={"status": "done"})
        assert authed.get(f"/api/orders/{order['id']}").json()["status"] == "printing"                                  # the first done: on its way
        authed.patch(f"/api/queue/{mine[1]['id']}", json={"status": "done"})
        assert authed.get(f"/api/orders/{order['id']}").json()["status"] == "ready"                                     # all done: ready
        authed.patch(f"/api/orders/{order['id']}", json={"status": "delivered"})
        authed.patch(f"/api/queue/{mine[1]['id']}", json={"status": "printing"})
        authed.patch(f"/api/queue/{mine[1]['id']}", json={"status": "done"})
        assert authed.get(f"/api/orders/{order['id']}").json()["status"] == "delivered"                                 # a delivered order is left alone
    finally:
        authed.delete(f"/api/orders/{order['id']}")


def test_only_the_estimate_exists_as_a_function_too():
    assert budgets.TOLERANCE < 0.01


# ---------------------------------------------------------------- a sliced file for one printer
def _keep_for(c, m, printer_id, data=GCODE, name="plate.gcode"):
    r = c.post("/api/print-files", data={"model_id": str(m["id"]), "printer_id": str(printer_id)}, files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_file_can_be_made_for_a_printer_and_is_sent_only_there(authed, farm):
    fake, a, b, m, _ = farm
    mine = _keep_for(authed, m, a["id"], b"G28\n; for alpha only\n")
    assert mine["printer_id"] == a["id"] and authed.get("/api/print-files", params={"model_id": m["id"]}).json()[0]["printer_id"] == a["id"]
    assert authed.post("/api/print-files", data={"model_id": str(m["id"]), "printer_id": "987654"}, files={"file": ("x.gcode", GCODE, "application/octet-stream")}).status_code == 400
    item_b = _queue(authed, farm, printer_id=b["id"])
    fake.moonraker_state = "standby"
    refused = authed.post(f"/api/queue/{item_b['id']}/send", json={"start": False})
    assert refused.status_code == 400 and "made for other printers" in refused.json()["detail"]
    item_a = _queue(authed, farm, printer_id=a["id"])
    assert authed.post(f"/api/queue/{item_a['id']}/send", json={"start": False}).status_code == 200
    assert b"for alpha only" in fake.uploads[-1][1]
    _keep_for(authed, m, b["id"], b"G28\n; for bravo only\n")
    assert authed.post(f"/api/queue/{item_b['id']}/send", json={"start": False}).status_code == 200
    assert b"for bravo only" in fake.uploads[-1][1]                                                                        # each printer got its own file


def test_a_file_that_suits_any_printer_is_the_fallback_and_the_printers_own_wins(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m, b"G28\n; generic\n", "generic.gcode")
    _keep_for(authed, m, a["id"], b"G28\n; alpha own\n", "own.gcode")
    fake.moonraker_state = "standby"
    for printer, wanted in ((a, b"alpha own"), (b, b"generic")):
        item = _queue(authed, farm, printer_id=printer["id"])
        assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200
        assert wanted in fake.uploads[-1][1]


def test_suggestions_only_name_printers_that_have_a_file_and_a_removed_printer_frees_its_file(authed, farm):
    fake, a, b, m, _ = farm
    keep = _keep_for(authed, m, a["id"])
    item = _queue(authed, farm)
    assert [c["name"] for c in authed.get("/api/queue/suggestions").json()[str(item["id"])]] == ["Alpha"]
    assert authed.patch(f"/api/print-files/{keep['id']}", json={"printer_id": b["id"]}).json()["printer_id"] == b["id"]
    assert authed.patch(f"/api/print-files/{keep['id']}", json={"printer_id": 987654}).status_code == 400
    assert [c["name"] for c in authed.get("/api/queue/suggestions").json()[str(item["id"])]] == ["Bravo"]
    authed.delete(f"/api/printers/{b['id']}")
    assert authed.get("/api/print-files", params={"model_id": m["id"]}).json()[0]["printer_id"] is None
    assert len(authed.get("/api/queue/suggestions").json()[str(item["id"])]) >= 1


# ---------------------------------------------------------------- an exact colour
@pytest.fixture()
def colours(authed, farm):
    fake, a, b, m, _ = farm
    authed.patch(f"/api/printers/{a['id']}", json={"slot_count": 1})
    authed.patch(f"/api/printers/{b['id']}", json={"slot_count": 1})
    red = authed.post("/api/filament", json={"material": "PLA", "color": "Red", "spool_weight_g": 1000, "remaining_g": 900}).json()
    blue = authed.post("/api/filament", json={"material": "PLA", "color": "Blue", "spool_weight_g": 1000, "remaining_g": 900}).json()
    authed.put(f"/api/slots/{a['id']}/1", json={"filament_id": blue["id"]})
    authed.put(f"/api/slots/{b['id']}/1", json={"filament_id": red["id"]})
    yield farm, red, blue
    for f in (red, blue):
        authed.delete(f"/api/filament/{f['id']}")
    authed.put("/api/settings", json={"strict_colour": ""})


def test_without_the_switch_a_different_colour_is_only_ranked_lower(authed, colours):
    farm_, red, blue = colours
    item = _queue(authed, farm_, filament_id=red["id"])
    names = [c["name"] for c in authed.get("/api/queue/suggestions").json()[str(item["id"])]]
    assert names[0] == "Bravo" and "Alpha" in names


def test_with_the_switch_only_a_printer_with_exactly_that_colour_is_suggested(authed, colours):
    farm_, red, blue = colours
    item = _queue(authed, farm_, filament_id=red["id"], strict_match=True)
    assert [c["name"] for c in authed.get("/api/queue/suggestions").json()[str(item["id"])]] == ["Bravo"]
    authed.patch(f"/api/queue/{item['id']}", json={"strict_match": None})
    assert len(authed.get("/api/queue/suggestions").json()[str(item["id"])]) == 2                                          # follows the setting again
    authed.put("/api/settings", json={"strict_colour": "true"})
    assert [c["name"] for c in authed.get("/api/queue/suggestions").json()[str(item["id"])]] == ["Bravo"]
    authed.patch(f"/api/queue/{item['id']}", json={"strict_match": False})
    assert len(authed.get("/api/queue/suggestions").json()[str(item["id"])]) == 2                                          # this print opts out
    assert authed.patch(f"/api/queue/{item['id']}", json={"strict_match": "yes"}).status_code == 400


def test_a_matching_colour_by_hex_counts_and_nothing_loaded_means_no_printer(authed, colours):
    farm_, red, blue = colours
    authed.patch(f"/api/filament/{red['id']}", json={"color": "Rot", "color_hex": "#ff0000"})
    twin = authed.post("/api/filament", json={"material": "PLA", "color": "Crimson", "color_hex": "#FF0000", "spool_weight_g": 1000, "remaining_g": 500}).json()
    try:
        item = _queue(authed, farm_, filament_id=twin["id"], strict_match=True)
        assert [c["name"] for c in authed.get("/api/queue/suggestions").json()[str(item["id"])]] == ["Bravo"]            # same hex, another name
        green = authed.post("/api/filament", json={"material": "PLA", "color": "Green", "spool_weight_g": 1000, "remaining_g": 500}).json()
        none = _queue(authed, farm_, filament_id=green["id"], strict_match=True)
        assert str(none["id"]) not in authed.get("/api/queue/suggestions").json()
        authed.delete(f"/api/filament/{green['id']}")
    finally:
        authed.delete(f"/api/filament/{twin['id']}")


def test_starting_on_a_printer_without_the_exact_colour_asks_first(authed, colours):
    farm_, red, blue = colours
    fake, a, b, m, _ = farm_
    _keep(authed, m)
    item = _queue(authed, farm_, printer_id=a["id"], filament_id=red["id"], strict_match=True)                              # Alpha holds blue
    fake.moonraker_state = "standby"
    r = authed.post(f"/api/queue/{item['id']}/send", json={"start": True})
    assert r.status_code == 409 and r.json()["detail"].startswith("Colour: ") and "Alpha" in r.json()["detail"]
    assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200
    assert "Colour:" not in authed.post(f"/api/queue/{item['id']}/send", json={"start": True, "force": True}).text
    good = _queue(authed, farm_, printer_id=b["id"], filament_id=red["id"], strict_match=True)                               # Bravo holds red
    assert "Colour:" not in authed.post(f"/api/queue/{good['id']}/send", json={"start": True}).text


# ---------------------------------------------------------------- the page
def test_the_controls_are_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    for element in ("budgets-box", "strict-colour"):
        assert f'id="{element}"' in html, element
    for needle in ("/api/budgets", "queue-centre", "queue-strict", "sliced-for", "order-centre", "cost_centre_id", "strict_match", "(Wait|Low|Colour): "):
        assert needle in js, needle
