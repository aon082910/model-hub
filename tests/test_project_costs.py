"""What a project costs: priced spools, parts, and electricity."""
import io

import pytest
from pypdf import PdfReader

from app.estimate import minutes_for_grams


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture(autouse=True)
def clean_settings(authed):
    yield
    authed.put("/api/settings", json={"cost_kwh_price": "", "cost_printer_watts": ""})


def _project_with(c, name, grams_by_spool, parts=()):
    model = c.post("/api/library/import", files={"file": (f"{name}.stl", _stl(40 + len(name)), "application/octet-stream")}).json()
    pid = c.post("/api/projects", json={"name": name}).json()["id"]
    c.post(f"/api/projects/{pid}/models/{model['id']}")
    for spool_id, grams in grams_by_spool:
        assert c.post(f"/api/projects/{pid}/models/{model['id']}/filament", json={"filament_id": spool_id, "grams": grams}).status_code == 200
    for part in parts:
        assert c.post(f"/api/projects/{pid}/parts", json=part).status_code == 200
    return pid


def test_a_spools_price_gives_the_filament_cost(authed):
    spool = authed.post("/api/filament", json={"material": "PLA", "spool_weight_g": 1000, "remaining_g": 1000, "cost": 20.0}).json()
    pid = _project_with(authed, "cost_filament", [(spool["id"], 250)])
    cost = authed.get(f"/api/projects/{pid}").json()["cost"]
    assert cost["filament"] == 5.0 and cost["filament_unpriced_g"] == 0 and cost["total"] == 5.0
    assert cost["electricity"] is None                                   # no electricity price set
    assert authed.patch(f"/api/filament/{spool['id']}", json={"cost": 30}).json()["cost"] == 30
    assert authed.get(f"/api/projects/{pid}").json()["cost"]["filament"] == 7.5


def test_spools_without_a_price_are_counted_not_guessed(authed):
    priced = authed.post("/api/filament", json={"material": "PLA", "spool_weight_g": 500, "remaining_g": 500, "cost": 10}).json()
    free = authed.post("/api/filament", json={"material": "PETG", "spool_weight_g": 1000, "remaining_g": 1000}).json()
    pid = _project_with(authed, "cost_unpriced", [(priced["id"], 100), (free["id"], 60)])
    cost = authed.get(f"/api/projects/{pid}").json()["cost"]
    assert cost["filament"] == 2.0 and cost["filament_unpriced_g"] == 60


def test_all_parts_count_even_those_you_already_own(authed):
    pid = _project_with(authed, "cost_parts", [], parts=[
        {"name": "ESP32", "quantity": 2, "quantity_owned": 2, "unit_cost": 5.5},
        {"name": "Screws", "quantity": 10, "quantity_owned": 0, "unit_cost": 0.1},
        {"name": "Mystery part", "quantity": 1}])
    p = authed.get(f"/api/projects/{pid}").json()
    assert p["cost"]["parts"] == 12.0 and p["cost"]["parts_unpriced"] == 1
    assert p["cost_needed"] == 1.0                                       # what is still to buy is unchanged


def test_electricity_needs_a_price_and_uses_the_printers_watts(authed):
    spool = authed.post("/api/filament", json={"material": "PLA", "spool_weight_g": 1000, "remaining_g": 1000, "cost": 20}).json()
    pid = _project_with(authed, "cost_power", [(spool["id"], 100)])
    authed.put("/api/settings", json={"cost_kwh_price": "0.20", "cost_printer_watts": "200"})
    cost = authed.get(f"/api/projects/{pid}").json()["cost"]
    hours = minutes_for_grams(100, "PLA") / 60
    assert cost["print_hours"] == round(hours, 1) and cost["printer_watts"] == 200
    assert cost["electricity"] == round(hours * 0.2 * 0.20, 2)
    assert cost["total"] == round(2.0 + cost["electricity"], 2)


def test_bad_electricity_settings_are_ignored(authed):
    spool = authed.post("/api/filament", json={"material": "PLA", "spool_weight_g": 1000, "remaining_g": 1000, "cost": 20}).json()
    pid = _project_with(authed, "cost_badsetting", [(spool["id"], 100)])
    authed.put("/api/settings", json={"cost_kwh_price": "lots", "cost_printer_watts": "-5"})
    cost = authed.get(f"/api/projects/{pid}").json()["cost"]
    assert cost["electricity"] is None and cost["printer_watts"] == 150          # back to the default


def test_the_minutes_for_grams_estimate_is_consistent():
    assert minutes_for_grams(0) == 0
    assert minutes_for_grams(200) == pytest.approx(2 * minutes_for_grams(100))
    assert minutes_for_grams(100, "ABS") > minutes_for_grams(100, "PLA")         # lighter plastic: more volume per gram
    assert minutes_for_grams(100, "Unobtainium") == minutes_for_grams(100, "PLA")


def test_the_pdf_shows_the_cost_when_there_is_one(authed):
    spool = authed.post("/api/filament", json={"material": "PLA", "spool_weight_g": 1000, "remaining_g": 1000, "cost": 20}).json()
    pid = _project_with(authed, "cost_pdf", [(spool["id"], 500)], parts=[{"name": "Gizmo", "quantity": 1, "unit_cost": 3}])
    authed.put("/api/settings", json={"cost_kwh_price": "0.1"})
    total = authed.get(f"/api/projects/{pid}").json()["cost"]["total"]
    r = authed.get(f"/api/projects/{pid}/export.pdf")
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(r.content)).pages)
    for needle in ("What it costs", "$10.00", "Electricity", f"${total:,.2f}"):
        assert needle in text, needle


def test_a_project_with_no_prices_has_no_cost_section(authed):
    pid = authed.post("/api/projects", json={"name": "cost_none"}).json()["id"]
    assert authed.get(f"/api/projects/{pid}").json()["cost"]["total"] == 0
    r = authed.get(f"/api/projects/{pid}/export.pdf")
    assert "What it costs" not in "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(r.content)).pages)
