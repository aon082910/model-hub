"""From the research round: filament from G-code length and a low-spool check, queue priority/hold/tags/ordering, spool presets from the
Open Filament Database, and importing a shop's order export."""
import io
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import Session

from app import filament_db, print_files
from app.db import engine
from app.models import QueueItem
from test_print_farm import farm, _keep, _queue  # noqa: F401  (the fixture and helpers)

PW = "a long enough password"


# ---------------------------------------------------------------- filament from the length used
def test_a_length_of_filament_becomes_grams_by_material():
    assert print_files.grams_from_length(1000, "PLA") == pytest.approx(2.98, abs=0.01)         # pi*(0.875)^2*1000mm = 2.405 cm3 at 1.24 g/cm3
    assert print_files.grams_from_length(1000, "PETG") > print_files.grams_from_length(1000, "ABS")
    assert print_files.grams_from_length(1000, "mystery") == print_files.grams_from_length(1000, "PLA")
    assert print_files.grams_from_length(1000, "PLA", 2.85) > print_files.grams_from_length(1000, "PLA", 1.75) * 2
    assert print_files.grams_from_length(0) is None and print_files.grams_from_length(-5) is None and print_files.grams_from_length(10, "PLA", 20) is None


def test_gcode_without_a_weight_gets_one_from_its_length():
    only_length = "; filament used [mm] = 1000.0\n; filament_type = PLA\n"
    assert print_files.parse_gcode_text(only_length)["est_grams"] == pytest.approx(2.98, abs=0.01)
    both = "; filament used [mm] = 1000.0\n; filament used [g] = 7.5\n"
    assert print_files.parse_gcode_text(both)["est_grams"] == 7.5                                # a weight the slicer wrote wins
    two = "; filament used [mm] = 1000.0, 500.0\n"
    assert print_files.parse_gcode_text(two)["est_grams"] == pytest.approx(2.98 * 1.5, abs=0.02)
    cura = ";Filament used: 2m\n"
    assert print_files.parse_gcode_text(cura)["est_grams"] == pytest.approx(2.98 * 2, abs=0.02)
    wide = "; filament used [mm] = 1000.0\n; filament_diameter = 2.85\n"
    assert print_files.parse_gcode_text(wide)["est_grams"] > 7
    assert print_files.parse_gcode_text("G28\n")["est_grams"] is None


# ---------------------------------------------------------------- queue: priority, hold, tags, order
def test_priority_hold_and_tag_are_checked_and_saved(authed, farm):
    fake, a, b, m, _ = farm
    item = _queue(authed, farm)
    assert item["priority"] is None and item["held"] is None and item["printer_tag"] is None
    patched = authed.patch(f"/api/queue/{item['id']}", json={"priority": 1, "held": True, "printer_tag": " Garage "}).json()
    assert (patched["priority"], patched["held"], patched["printer_tag"]) == (1, True, "garage")
    for bad in ({"priority": 2}, {"priority": "high"}, {"priority": True}, {"held": "yes"}, {"printer_tag": "no/slash"}, {"printer_tag": "x" * 30}):
        assert authed.patch(f"/api/queue/{item['id']}", json=bad).status_code == 400, bad
    cleared = authed.patch(f"/api/queue/{item['id']}", json={"priority": 0, "held": False, "printer_tag": ""}).json()
    assert cleared["priority"] is None and not cleared["held"] and cleared["printer_tag"] is None
    assert _queue(authed, farm, priority=-1, printer_tag="Box")["priority"] == -1


def test_printers_have_tags_and_a_tagged_entry_goes_only_there(authed, farm):
    fake, a, b, m, _ = farm
    assert a["tags"] == []
    tagged = authed.patch(f"/api/printers/{a['id']}", json={"tags": "Garage, garage, Big nozzle"}).json()
    assert tagged["tags"] == ["garage", "big nozzle"]
    assert authed.patch(f"/api/printers/{a['id']}", json={"tags": ["a/b"]}).status_code == 400
    assert authed.patch(f"/api/printers/{a['id']}", json={"tags": [str(i) for i in range(9)]}).status_code == 400
    authed.patch(f"/api/printers/{b['id']}", json={"tags": "office"})
    item = _queue(authed, farm, printer_tag="office")
    got = authed.get("/api/queue/suggestions").json()[str(item["id"])]
    assert [c["name"] for c in got] == ["Bravo"]                                                 # Alpha is not tagged office
    assert authed.get("/api/queue/suggestions").status_code == 200
    authed.patch(f"/api/printers/{b['id']}", json={"tags": ""})
    assert str(item["id"]) not in authed.get("/api/queue/suggestions").json()                  # no printer carries the tag at all


def test_a_held_entry_is_not_suggested_or_sent(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    item = _queue(authed, farm, printer_id=a["id"])
    unassigned = _queue(authed, farm)
    authed.patch(f"/api/queue/{unassigned['id']}", json={"held": True})
    assert str(unassigned["id"]) not in authed.get("/api/queue/suggestions").json()
    authed.patch(f"/api/queue/{item['id']}", json={"held": True})
    fake.moonraker_state = "standby"
    r = authed.post(f"/api/queue/{item['id']}/send", json={"start": False})
    assert r.status_code == 409 and "on hold" in r.json()["detail"]
    authed.patch(f"/api/queue/{item['id']}", json={"held": False})
    assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200


def test_an_entry_will_not_go_to_a_printer_without_its_tag(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    item = _queue(authed, farm, printer_id=a["id"], printer_tag="office")
    fake.moonraker_state = "standby"
    r = authed.post(f"/api/queue/{item['id']}/send", json={"start": False})
    assert r.status_code == 409 and "tagged" in r.json()["detail"]
    authed.patch(f"/api/printers/{a['id']}", json={"tags": "office"})
    assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200


def _order_of(authed, ids):
    return [i["id"] for i in sorted((q for q in authed.get("/api/queue").json() if q["id"] in ids), key=lambda q: q["position"])]


def test_sorting_by_priority_then_shortest_first_and_old_entries_are_not_starved(authed, farm):
    fake, a, b, m, _ = farm
    long_one = _queue(authed, farm, estimated_minutes=300)
    short = _queue(authed, farm, estimated_minutes=10)
    middle = _queue(authed, farm, estimated_minutes=60)
    urgent = _queue(authed, farm, estimated_minutes=500, priority=1)
    ids = {long_one["id"], short["id"], middle["id"], urgent["id"]}
    assert authed.post("/api/queue/sort", json={"mode": "nonsense"}).status_code == 400
    r = authed.post("/api/queue/sort", json={"mode": "priority"}).json()
    assert _order_of(authed, ids) == [urgent["id"], long_one["id"], short["id"], middle["id"]]    # high first, the rest as added
    assert isinstance(r["moved"], int)
    authed.post("/api/queue/sort", json={"mode": "shortest"})
    assert _order_of(authed, ids) == [urgent["id"], short["id"], middle["id"], long_one["id"]]
    with Session(engine) as s:                                                                 # the long one has waited three days
        row = s.get(QueueItem, long_one["id"])
        row.created_at = datetime.utcnow() - timedelta(days=3)
        s.add(row)
        s.commit()
    authed.post("/api/queue/sort", json={"mode": "shortest"})
    assert _order_of(authed, ids) == [urgent["id"], long_one["id"], short["id"], middle["id"]]
    positions = [q["position"] for q in authed.get("/api/queue").json()]
    assert len(positions) == len(set(positions))                                                 # no two entries share a place


def test_starting_with_too_little_filament_is_refused_unless_forced(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    spool = authed.post("/api/filament", json={"material": "PLA", "brand": "Tiny", "color": "red", "spool_weight_g": 1000, "remaining_g": 20}).json()
    try:
        item = _queue(authed, farm, printer_id=a["id"], filament_id=spool["id"], estimated_grams=80)
        fake.moonraker_state = "standby"
        r = authed.post(f"/api/queue/{item['id']}/send", json={"start": True})
        assert r.status_code == 409 and r.json()["detail"].startswith("Low: ") and "20 g left" in r.json()["detail"] and "80 g" in r.json()["detail"]
        assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200      # only starting is checked
        forced = authed.post(f"/api/queue/{item['id']}/send", json={"start": True, "force": True})
        assert "Low:" not in forced.text
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


def test_a_printer_whose_loaded_spool_is_nearly_empty_is_suggested_last(authed, farm):
    fake, a, b, m, _ = farm
    authed.patch(f"/api/printers/{a['id']}", json={"slot_count": 1})
    authed.patch(f"/api/printers/{b['id']}", json={"slot_count": 1})
    low = authed.post("/api/filament", json={"material": "PLA", "color": "red", "spool_weight_g": 1000, "remaining_g": 10}).json()
    full = authed.post("/api/filament", json={"material": "PLA", "color": "red", "spool_weight_g": 1000, "remaining_g": 900}).json()
    try:
        authed.put(f"/api/slots/{a['id']}/1", json={"filament_id": low["id"]})
        authed.put(f"/api/slots/{b['id']}/1", json={"filament_id": full["id"]})
        item = _queue(authed, farm, filament_id=low["id"], estimated_grams=100)
        first = authed.get("/api/queue/suggestions").json()[str(item["id"])][0]
        assert first["name"] != "Alpha" or any("left" in r for r in first["reasons"])
        alpha = next(c for c in authed.get("/api/queue/suggestions").json()[str(item["id"])] if c["name"] == "Alpha")
        assert any("only 10 g of it is left" in r for r in alpha["reasons"])
    finally:
        for f in (low, full):
            authed.delete(f"/api/filament/{f['id']}")


# ---------------------------------------------------------------- the Open Filament Database
BRANDS = "id,uuid,name,slug,website,logo_name,origin,source,moved_from\nb1,u1,Acme,acme,,,US,,\nb2,u2,Zed Plastics,zed,,,DE,,\n"
FILAMENTS = ("id,uuid,brand_id,material_id,name,slug,material,density,diameter_tolerance,discontinued,certifications,chamber_temperature,data_sheet_url,max_bed_temperature,"
             "max_chamber_temperature,max_dry_temperature,max_print_temperature,min_bed_temperature,min_chamber_temperature,min_nozzle_diameter,min_print_temperature\n"
             "f1,fu1,b1,m1,Acme Silk PLA,acme_silk,PLA,1.24,0.02,0,,,,60,,,220,50,,,190\n"
             "f2,fu2,b2,m2,Zed Tough PETG,zed_tough,PETG,1.27,0.02,0,,,,80,,,250,70,,,230\n"
             "f3,fu3,b1,m1,Acme Old PLA,acme_old,PLA,1.24,0.02,1,,,,60,,,220,50,,,190\n")
VARIANTS = ("id,uuid,filament_id,slug,name,color_hex,hex_variants,color_standards,traits,discontinued,moved_from\n"
            "v1,vu1,f1,red,Fire Red,#FF0000,,,,0,\nv2,vu2,f1,blue,Ocean Blue,not-a-colour,,,,0,\nv3,vu3,f2,black,Black,#000000,,,,0,\nv4,vu4,f1,gone,Gone,#111111,,,,1,\nv5,vu5,f3,x,Old,#222222,,,,0,\n")
SIZES = ("id,uuid,variant_id,filament_weight,diameter,empty_spool_weight,spool_core_diameter,gtin,article_number,discontinued\n"
         "s1,su1,v1,1000,1.75,200,,,,0\ns2,su2,v1,250,1.75,100,,,,0\ns3,su3,v1,500,1.75,150,,,,1\ns4,su4,v3,750,1.75,180,,,,0\n")


@pytest.fixture()
def offline_db(monkeypatch, tmp_path):
    files = {"brands": BRANDS, "filaments": FILAMENTS, "variants": VARIANTS, "sizes": SIZES}
    seen = []

    def handler(request):
        seen.append(str(request.url))
        name = request.url.path.rsplit("/", 1)[-1].replace(".csv", "")
        return httpx.Response(200, text=files[name]) if request.url.host == "api.openfilamentdatabase.org" and name in files else httpx.Response(404)
    monkeypatch.setattr(filament_db, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(filament_db, "CACHE", tmp_path / "filament_db.json")
    return seen


def test_nothing_is_searchable_until_the_database_is_downloaded(authed, offline_db):
    assert authed.get("/api/filament/db/status").json() == {"present": False, "colours": 0, "age_days": None, "old": False}
    assert authed.get("/api/filament/db/search", params={"q": "acme"}).status_code == 404


def test_the_download_is_turned_into_searchable_colours(authed, offline_db):
    status = authed.post("/api/filament/db/update").json()
    assert status["present"] is True and status["colours"] == 3 and status["old"] is False                       # discontinued ones are left out
    assert sorted(offline_db) == sorted(f"https://api.openfilamentdatabase.org/csv/{n}.csv" for n in ("brands", "filaments", "variants", "sizes"))
    red = authed.get("/api/filament/db/search", params={"q": "acme red"}).json()["results"]
    assert red == [{"brand": "Acme", "name": "Acme Silk PLA", "material": "PLA", "color": "Fire Red", "hex": "#FF0000", "grams": [250, 1000],
                    "density": 1.24, "nozzle": [190.0, 220.0], "bed": [50.0, 60.0]}]
    blue = authed.get("/api/filament/db/search", params={"q": "ocean"}).json()["results"][0]
    assert blue["hex"] is None and blue["grams"] == []                                                          # a bad colour is not trusted
    assert {r["color"] for r in authed.get("/api/filament/db/search", params={"q": "petg zed"}).json()["results"]} == {"Black"}
    assert authed.get("/api/filament/db/search", params={"q": ""}).json() == {"results": []}
    assert authed.get("/api/filament/db/search", params={"q": "nothing like this"}).json() == {"results": []}
    assert len(authed.get("/api/filament/db/search", params={"q": "a", "limit": 1}).json()["results"]) == 1


def test_a_failing_download_changes_nothing(authed, offline_db, monkeypatch):
    authed.post("/api/filament/db/update")
    monkeypatch.setattr(filament_db, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    r = authed.post("/api/filament/db/update")
    assert r.status_code == 502 and "did not give" in r.json()["detail"]
    assert authed.get("/api/filament/db/status").json()["colours"] == 3                                         # the old list is still there

    def broken(request):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(filament_db, "_client", lambda: httpx.Client(transport=httpx.MockTransport(broken)))
    assert authed.post("/api/filament/db/update").status_code == 502


def test_an_old_list_is_said_to_be_old(authed, offline_db):
    import json
    authed.post("/api/filament/db/update")
    data = json.loads(filament_db.CACHE.read_text(encoding="utf-8"))
    data["fetched_at"] -= 90 * 86400
    filament_db.CACHE.write_text(json.dumps(data), encoding="utf-8")
    assert authed.get("/api/filament/db/status").json()["old"] is True


# ---------------------------------------------------------------- importing a shop's orders
def _model(c, name):
    stl = f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 30 0 0\n  vertex 0 {20 + uuid.uuid4().int % 50} 0\n endloop\nendfacet\nendsolid t\n".encode()
    r = c.post("/api/library/import", files={"file": (name, stl, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


SHOPIFY = ("Name,Email,Created at,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku,Shipping Name\n"
           "#{n}1,pat@example.com,2026-10-01,2,Dragon Egg - large,12.50,{sku},Pat Example\n"
           "#{n}1,pat@example.com,2026-10-01,1,Mystery gift,5,,Pat Example\n"
           "#{n}2,sam@example.com,2026-10-02,3,Phone stand,\"$7,50\",,Sam Other\n")


def test_a_shop_export_becomes_orders_and_nothing_is_lost(authed):
    tag = uuid.uuid4().hex[:6]
    egg = _model(authed, f"dragonegg{tag}.stl")
    stand = _model(authed, f"Phone Stand {tag}.stl")
    n = uuid.uuid4().int % 90000 + 10000
    csv_text = SHOPIFY.format(n=n, sku=f"dragonegg{tag}").replace("Phone stand", f"Phone stand {tag}")
    try:
        dry = authed.post("/api/orders/import?dry=true", files={"file": ("orders.csv", csv_text.encode(), "text/csv")}).json()
        assert dry["dry"] is True and len(dry["orders"]) == 2 and dry["lines"] == 3 and dry["matched"] == 2
        assert [o for o in authed.get("/api/orders").json()["orders"] if o["customer"] in ("Pat Example", "Sam Other")] == []     # a dry run writes nothing
        done = authed.post("/api/orders/import", files={"file": ("orders.csv", csv_text.encode(), "text/csv")}).json()
        first = next(o for o in done["orders"] if o["customer"] == "Pat Example")
        assert first["matched"] == [{"filename": egg["filename"], "quantity": 2, "unit_price": 12.5}] and first["unmatched"] == ["Mystery gift x 1"]
        second = next(o for o in done["orders"] if o["customer"] == "Sam Other")
        assert second["matched"][0]["filename"] == stand["filename"] and second["matched"][0]["quantity"] == 3 and second["matched"][0]["unit_price"] == 7.5
        listed = {o["customer"]: o for o in authed.get("/api/orders").json()["orders"]}
        pat = authed.get(f"/api/orders/{listed['Pat Example']['id']}").json()
        assert pat["status"] == "accepted" and pat["contact"] == "pat@example.com" and "Mystery gift x 1" in pat["notes"] and len(pat["items"]) == 1
        again = authed.post("/api/orders/import", files={"file": ("orders.csv", csv_text.encode(), "text/csv")}).json()
        assert again["orders"] == [] and len(again["skipped"]) == 2                                          # the same export twice makes no copies
    finally:
        for o in authed.get("/api/orders").json()["orders"]:
            if o["customer"] in ("Pat Example", "Sam Other"):
                authed.delete(f"/api/orders/{o['id']}")
        for m in (egg, stand):
            authed.delete(f"/api/library/models/{m['id']}")


def test_other_shops_column_names_are_understood(authed):
    tag = uuid.uuid4().hex[:6]
    model = _model(authed, f"Wall Hook {tag}.stl")
    etsy = f"Sale Date,Item Name,Buyer,Quantity,Price,Order ID,SKU\n10/01/2026,Wall Hook {tag},Ann Buyer,4,6.00,E{tag},\n"
    try:
        r = authed.post("/api/orders/import", files={"file": ("etsy.csv", etsy.encode(), "text/csv")}).json()
        assert r["orders"][0]["customer"] == "Ann Buyer" and r["orders"][0]["matched"][0]["quantity"] == 4
    finally:
        for o in authed.get("/api/orders").json()["orders"]:
            if o["customer"] == "Ann Buyer":
                authed.delete(f"/api/orders/{o['id']}")
        authed.delete(f"/api/library/models/{model['id']}")


def test_bad_exports_are_refused_and_ambiguous_titles_are_not_guessed(authed):
    assert authed.post("/api/orders/import", files={"file": ("x.csv", b"Colour,Size\nred,big\n", "text/csv")}).status_code == 400
    assert authed.post("/api/orders/import", files={"file": ("x.csv", b"a" * (2 * 1024 * 1024 + 5), "text/csv")}).status_code == 400
    assert authed.post("/api/orders/import").status_code == 400
    tag = uuid.uuid4().hex[:6]
    one, two = _model(authed, f"Gear Small {tag}.stl"), _model(authed, f"Gear Large {tag}.stl")
    try:
        ambiguous = f"Order ID,Item Name,Quantity,Buyer\nZ{tag},gear {tag},1,Nobody Twice\n"
        r = authed.post("/api/orders/import?dry=true", files={"file": ("a.csv", ambiguous.encode(), "text/csv")}).json()
        assert r["orders"][0]["matched"] == [] and r["orders"][0]["unmatched"] == [f"gear {tag} x 1"]
    finally:
        for m in (one, two):
            authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- the page
def test_the_new_controls_are_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    for element in ("fil-db-box", "fil-db-q", "fil-db-search", "fil-db-update", "fil-db-results", "queue-sort-priority", "queue-sort-shortest",
                    "order-import-file", "order-import-preview", "order-import-go", "order-import-result"):
        assert f'id="{element}"' in html, element
    for needle in ("/api/filament/db/search", "/api/filament/db/update", "color_hex", "queue-priority", "queue-hold", "queue-tag", "/api/queue/sort",
                   "printer-tags-save", "/api/orders/import", "(Wait|Low): "):
        assert needle in js, needle
