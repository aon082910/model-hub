"""Supplies-on-hand inventory, and the shopping list's export / combine / in-stock hint."""
import csv
import io


def _item(client, name, **extra):
    r = client.post("/api/inventory", json={"name": name, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _project_with_part(client, project_name, part_name, **part):
    pid = client.post("/api/projects", json={"name": project_name}).json()["id"]
    r = client.post(f"/api/projects/{pid}/parts", json={"name": part_name, **part})
    assert r.status_code == 200, r.text
    return pid


def test_inventory_requires_auth(client):
    client.post("/api/auth/logout")
    assert client.get("/api/inventory").status_code == 401
    assert client.get("/api/projects/shopping-list/export").status_code == 401


def test_inventory_crud_and_validation(authed):
    c = authed
    item = _item(c, "  Inv ESP32  ", category="electronics", quantity=5, min_quantity=2, location="Bin 3", unit_cost=7.5)
    assert item["name"] == "Inv ESP32" and item["low_stock"] is False

    bad = [
        {"name": ""}, {"name": "x", "category": "weapons"}, {"name": "x", "quantity": -1},
        {"name": "x", "quantity": "many"}, {"name": "x", "min_quantity": -3}, {"name": "x", "unit_cost": -1},
    ]
    for payload in bad:
        assert c.post("/api/inventory", json=payload).status_code == 400, payload

    patched = c.patch(f"/api/inventory/{item['id']}", json={"quantity": 2}).json()
    assert patched["quantity"] == 2 and patched["low_stock"] is True   # at/below the minimum
    assert c.patch(f"/api/inventory/{item['id']}", json={"quantity": -1}).status_code == 400
    assert c.patch("/api/inventory/999999", json={"quantity": 1}).status_code == 404

    assert c.delete(f"/api/inventory/{item['id']}").status_code == 200
    assert c.delete(f"/api/inventory/{item['id']}").status_code == 404


def test_inventory_filters(authed):
    c = authed
    _item(c, "Filt resistor 10k", category="electronics", quantity=100)
    _item(c, "Filt M3 screw", category="parts", quantity=1, min_quantity=5, location="Drawer A")
    _item(c, "Filt solder", category="supplies", quantity=1)

    names = lambda r: {i["name"] for i in r.json() if i["name"].startswith("Filt")}
    assert names(c.get("/api/inventory?category=parts")) == {"Filt M3 screw"}
    assert names(c.get("/api/inventory?q=drawer")) == {"Filt M3 screw"}      # matches location
    assert names(c.get("/api/inventory?low_stock=true")) == {"Filt M3 screw"}
    assert c.get("/api/inventory?category=bogus").status_code == 400


def test_inventory_export_csv(authed):
    c = authed
    _item(c, "=SUM(A1) export", category="parts", quantity=3)
    r = c.get("/api/inventory/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:3] == ["Name", "Type", "Quantity"]
    # a name starting with = must not become a formula in a spreadsheet
    assert any(row[0] == "'=SUM(A1) export" for row in rows)


def test_shopping_list_in_stock_hint_and_combine(authed):
    c = authed
    _item(c, "Combo servo", category="electronics", quantity=4)
    p1 = _project_with_part(c, "Combo A", "Combo Servo", category="electronics", quantity=2, unit_cost=3.0)
    p2 = _project_with_part(c, "Combo B", "combo servo", category="electronics", quantity=3, quantity_owned=1)

    lines = [i for i in c.get("/api/projects/shopping-list").json()["items"] if i["name"].lower() == "combo servo"]
    assert len(lines) == 2
    assert all(i["in_stock"] == 4 for i in lines)             # same-named inventory, any letter case

    combined = [i for i in c.get("/api/projects/shopping-list?combine=true").json()["items"]
                if i["name"].lower() == "combo servo"]
    assert len(combined) == 1
    assert combined[0]["quantity_needed"] == 4                # 2 + (3 - 1)
    assert combined[0]["cost_needed"] == 12.0                 # cost taken from the line that had one
    assert set(combined[0]["project_names"]) == {"Combo A", "Combo B"}
    assert p1 != p2


def test_shopping_list_export_csv_and_txt(authed):
    c = authed
    _project_with_part(c, "Export proj", "Export LED", category="electronics", quantity=2, unit_cost=1.25,
                       purchase_url="https://example.com/led")
    _project_with_part(c, "Export proj 2", "-cmd|calc", category="supplies", quantity=1)

    csv_r = c.get("/api/projects/shopping-list/export?format=csv")
    assert csv_r.status_code == 200 and csv_r.headers["content-type"].startswith("text/csv")
    assert 'filename="shopping-list-' in csv_r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(csv_r.text)))
    assert rows[0] == ["Part", "Type", "Quantity", "Unit cost", "Line cost", "In stock", "Projects", "Link"]
    led = next(r for r in rows if r and r[0] == "Export LED")
    assert led[2:5] == ["2", "1.25", "2.50"] and led[6] == "Export proj"
    assert any(r and r[0] == "'-cmd|calc" for r in rows)       # formula-looking text neutralised
    assert any(r and r[0] == "Estimated total" for r in rows)

    txt = c.get("/api/projects/shopping-list/export?format=txt")
    assert txt.status_code == 200 and txt.headers["content-type"].startswith("text/plain")
    assert "ELECTRONICS" in txt.text and "[ ] 2 x Export LED - $2.50" in txt.text
    assert "https://example.com/led" in txt.text

    assert c.get("/api/projects/shopping-list/export?format=pdf").status_code == 400


def test_export_skips_done_projects(authed):
    c = authed
    pid = _project_with_part(c, "Export finished", "Export ghost part", category="parts", quantity=1)
    c.patch(f"/api/projects/{pid}", json={"status": "done"})
    assert "Export ghost part" not in c.get("/api/projects/shopping-list/export?format=txt").text
