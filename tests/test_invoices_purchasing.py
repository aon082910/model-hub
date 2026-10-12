"""Invoices, payments, customer terms and credit limits, suggested prices, and purchasing (vendors, purchase orders, receiving, the buy list)."""
import uuid
from datetime import date, timedelta

import pytest
from sqlmodel import Session, select
from starlette.testclient import TestClient

from app import auth, costing, invoices, purchasing
from app.db import engine
from app.models import Filament, InventoryItem, Invoice, Order

PW = "a long enough password"
SETTING_KEYS = ("shop_name", "shop_address", "tax_rate_pct", "invoice_prefix", "invoice_footer", "quote_currency", "cost_margin_pct", "cost_default_per_kg", "cost_machine_per_hour", "low_filament_g", "notify_invoice_overdue")


def _app():
    from app.main import app
    return app


def _stl():
    n = 30 + uuid.uuid4().int % 300
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 5}\n endloop\nendfacet\nendsolid t\n").encode() + uuid.uuid4().hex.encode()


def _model(c):
    r = c.post("/api/library/import", files={"file": (f"inv_{uuid.uuid4().hex[:6]}_widget.stl", _stl(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _tidy(authed):
    yield
    authed.put("/api/settings", json={k: "" for k in SETTING_KEYS})
    with Session(engine) as s:
        from app.models import CustomerTerms, Payment, PurchaseLine, PurchaseOrder, Vendor
        for table in (Payment, Invoice, CustomerTerms, PurchaseLine, PurchaseOrder, Vendor):
            for row in s.exec(select(table)).all():
                s.delete(row)
        s.commit()
    for o in authed.get("/api/orders").json()["orders"]:
        authed.delete(f"/api/orders/{o['id']}")
    for u in authed.get("/api/users").json()["users"]:
        authed.delete(f"/api/users/{u['id']}")
    for g in authed.get("/api/access-groups").json()["groups"]:
        authed.delete(f"/api/access-groups/{g['id']}")


def _order(c, customer="Acme Co", prices=(10.0,), quantity=2, status=None):
    m = _model(c)
    items = [{"model_id": m["id"], "quantity": quantity, "unit_price": p} for p in prices]
    o = c.post("/api/orders", json={"customer": customer, "contact": "buyer@acme.example", "items": items}).json()
    if status:
        c.patch(f"/api/orders/{o['id']}", json={"status": status})
    return o


def _invoice(c, order, **body):
    return c.post(f"/api/invoices/from-order/{order['id']}", json=body)


# ---------------------------------------------------------------- invoices
def test_an_invoice_is_a_snapshot_with_a_number_terms_and_tax(authed):
    authed.put("/api/settings", json={"tax_rate_pct": "10", "invoice_prefix": "SH-", "quote_currency": "$"})
    o = _order(authed, prices=(10.0, 5.5), quantity=2)
    r = _invoice(authed, o, terms="net_30", notes="Thanks")
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["number"] == "SH-0001" and inv["terms"] == "net_30" and inv["terms_label"] == "Net 30 days" and inv["notes"] == "Thanks"
    assert inv["subtotal"] == 31.0 and inv["tax"] == 3.1 and inv["total"] == 34.1 and inv["balance"] == 34.1 and inv["status"] == "issued" and inv["overdue"] is False
    assert date.fromisoformat(inv["due_on"]) - date.fromisoformat(inv["issued_on"]) == timedelta(days=30)
    assert [l["unit"] for l in inv["lines"]] == [10.0, 5.5] and inv["lines"][0]["name"].endswith("widget") and "_" not in inv["lines"][0]["name"]
    authed.patch(f"/api/orders/{o['id']}/items/{authed.get('/api/orders/%d' % o['id']).json()['items'][0]['id']}", json={"unit_price": 99})
    assert authed.get(f"/api/invoices/{inv['id']}").json()["total"] == 34.1                                                          # later changes do not alter it
    assert authed.get(f"/api/orders/{o['id']}").json()["invoice"] == {"id": inv["id"], "number": "SH-0001", "status": "issued", "total": 34.1, "balance": 34.1, "due_on": inv["due_on"]}
    assert _invoice(authed, o).status_code == 409 and "already has an invoice" in _invoice(authed, o).json()["detail"]
    other = _order(authed, "Other")
    assert _invoice(authed, other).json()["number"] == "SH-0002"
    assert _invoice(authed, other).status_code == 409 and _invoice(authed, {"id": 999999}).status_code == 404


def test_what_cannot_be_invoiced_is_said(authed):
    m = _model(authed)
    nothing = authed.post("/api/orders", json={"customer": "Empty"}).json()
    assert "nothing to invoice" in _invoice(authed, nothing).json()["detail"]
    unpriced = authed.post("/api/orders", json={"customer": "NoPrice", "items": [{"model_id": m["id"], "quantity": 1}]}).json()
    r = _invoice(authed, unpriced)
    assert r.status_code == 400 and r.json()["detail"].startswith("Give a price to: inv_")
    cancelled = _order(authed, "Gone", status="cancelled")
    assert "cancelled" in _invoice(authed, cancelled).json()["detail"]
    ok = _order(authed, "Bad Terms")
    assert _invoice(authed, ok, terms="net_99").status_code == 400 and _invoice(authed, ok, terms=5).status_code == 400 and _invoice(authed, ok, notes=5).status_code == 400


def test_payments_settle_the_invoice_and_the_order_and_can_be_taken_back(authed):
    o = _order(authed, prices=(10.0,), quantity=3)
    inv = _invoice(authed, o).json()
    assert inv["total"] == 30.0 and inv["due_on"] == inv["issued_on"]
    r = authed.post(f"/api/invoices/{inv['id']}/payments", json={"amount": 12.5, "method": "cash", "paid_on": "2026-10-01", "note": "deposit"})
    assert r.status_code == 200 and r.json()["paid"] == 12.5 and r.json()["balance"] == 17.5 and r.json()["status"] == "issued" and r.json()["payments"][0]["method"] == "cash"
    assert authed.get(f"/api/orders/{o['id']}").json()["paid"] is False
    for bad in ({"amount": 0}, {"amount": -1}, {"amount": True}, {"amount": "5"}, {"amount": 17.51}, {"amount": 5, "paid_on": "yesterday"}, {}):
        assert authed.post(f"/api/invoices/{inv['id']}/payments", json=bad).status_code == 400, bad
    done = authed.post(f"/api/invoices/{inv['id']}/payments", json={"amount": 17.5}).json()
    assert done["status"] == "paid" and done["balance"] == 0 and authed.get(f"/api/orders/{o['id']}").json()["paid"] is True
    pid = done["payments"][-1]["id"]
    back = authed.delete(f"/api/invoices/{inv['id']}/payments/{pid}").json()
    assert back["status"] == "issued" and back["balance"] == 17.5 and authed.get(f"/api/orders/{o['id']}").json()["paid"] is False
    assert authed.delete(f"/api/invoices/{inv['id']}/payments/999999").status_code == 404
    authed.post(f"/api/invoices/{inv['id']}/payments", json={"amount": 17.5})
    voided = authed.post(f"/api/invoices/{inv['id']}/void").json()
    assert voided["status"] == "void" and voided["balance"] == 0 and authed.get(f"/api/orders/{o['id']}").json()["paid"] is False
    assert authed.post(f"/api/invoices/{inv['id']}/payments", json={"amount": 1}).status_code == 400
    again = _invoice(authed, o)
    assert again.status_code == 200 and again.json()["number"] != inv["number"]                                                       # a void invoice frees the order for a new one


def test_customer_terms_set_the_due_date_a_discount_and_a_credit_limit(authed):
    assert authed.put("/api/customers", json={"name": "Acme Co", "payment_terms": "net_15", "credit_limit": 100, "discount_pct": 10, "notes": "good payer"}).json()["payment_terms"] == "net_15"
    for bad in ({"name": ""}, {"name": "X", "payment_terms": "whenever"}, {"name": "X", "credit_limit": -1}, {"name": "X", "discount_pct": 101}, {"name": "X", "credit_limit": True}, {"name": "X", "notes": 5}, {}):
        assert authed.put("/api/customers", json=bad).status_code == 400, bad
    first = _order(authed, "  ACME   co ", prices=(50.0,), quantity=2)                                                              # spelled differently, same customer
    inv = _invoice(authed, first).json()
    assert inv["terms"] == "net_15" and inv["customer_discount_pct"] == 10 and inv["subtotal"] == 100.0 and inv["customer_discount"] == 10.0 and inv["total"] == 90.0
    second = _order(authed, "Acme Co", prices=(20.0,), quantity=1)
    r = _invoice(authed, second)
    assert r.status_code == 409 and r.json()["detail"].startswith("Credit limit: ") and "90.00" in r.json()["detail"]
    assert _invoice(authed, second, force=True).status_code == 200                                                                   # going on anyway is a choice
    third = _order(authed, "Acme Co", prices=(1.0,), quantity=1)
    authed.post(f"/api/invoices/{inv['id']}/payments", json={"amount": 90})
    c = {x["name"]: x for x in authed.get("/api/customers").json()["customers"]}
    assert c["Acme Co"]["owes"] == 18.0 and c["Acme Co"]["credit_limit"] == 100.0 and "Acme Co" in c
    assert _invoice(authed, third).status_code == 200                                                                                # paying frees credit
    assert authed.put("/api/customers", json={"name": "acme CO", "payment_terms": "net_60"}).json()["credit_limit"] is None          # the same customer: updated, not duplicated
    assert len([x for x in authed.get("/api/customers").json()["customers"] if x["name"].lower() == "acme co"]) == 1


def test_invoices_can_be_listed_and_an_overdue_one_is_told_about_once(authed, monkeypatch):
    o = _order(authed, prices=(40.0,), quantity=1)
    with Session(engine) as s:
        invoice = invoices.create(s, s.get(Order, o["id"]), terms="net_15", today=date.today() - timedelta(days=40))
        number = invoice.number
    assert authed.get("/api/invoices", params={"status": "issued"}).json()["overdue"] == 1
    listing = authed.get("/api/invoices").json()
    assert listing["owed"] == 40.0 and listing["invoices"][0]["overdue"] is True and listing["invoices"][0]["number"] == number
    assert authed.get("/api/invoices", params={"status": "paid"}).json()["invoices"] == [] and authed.get("/api/invoices", params={"status": "nope"}).status_code == 400
    assert len(authed.get("/api/invoices", params={"customer": "ACME CO"}).json()["invoices"]) == 1 and authed.get("/api/invoices", params={"customer": "nobody"}).json()["invoices"] == []
    told = []
    import app.notify as notify
    monkeypatch.setattr(notify, "notify_event", lambda session, event, title, message, **kw: told.append((event, title, message)))
    with Session(engine) as s:
        assert invoices.check_overdue(s) == [number] and invoices.check_overdue(s) == []
    assert told[0][0] == "invoice_overdue" and number in told[0][1] and "40.00" in told[0][2]
    from app import scheduler
    assert "invoices" in scheduler.INTERVALS and "invoices" in scheduler._jobs()


def test_the_invoice_pdf_is_a_pdf_with_what_it_should_say(authed):
    authed.put("/api/settings", json={"shop_name": "Maker Corner", "shop_address": "1 Print St\nTown", "invoice_footer": "Pay by bank transfer", "quote_currency": "$"})
    o = _order(authed, "Zed <b>&</b> Co", prices=(7.0,), quantity=3)
    inv = _invoice(authed, o).json()
    r = authed.get(f"/api/invoices/{inv['id']}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF") and inv["number"] in r.headers["content-disposition"] and len(r.content) > 1500
    assert authed.get("/api/invoices/999999/pdf").status_code == 404


def test_a_line_discount_and_suggested_prices(authed):
    o = _order(authed, prices=(20.0,), quantity=2)
    item_id = authed.get(f"/api/orders/{o['id']}").json()["items"][0]["id"]
    got = authed.patch(f"/api/orders/{o['id']}/items/{item_id}", json={"discount_pct": 25}).json()["items"][0]
    assert got["discount_pct"] == 25 and got["line_price"] == 30.0
    for bad in (101, -5, True, "ten"):
        assert authed.patch(f"/api/orders/{o['id']}/items/{item_id}", json={"discount_pct": bad}).status_code == 400, bad
    assert _invoice(authed, o).json()["lines"][0]["discount_pct"] == 25
    assert authed.patch(f"/api/orders/{o['id']}/items/{item_id}", json={"discount_pct": 0}).json()["items"][0]["discount_pct"] is None
    # suggested prices: cost plus a margin, only for items with no price
    m = _model(authed)
    authed.post("/api/prints", json={"model_id": m["id"], "grams": 50, "minutes": 60, "deduct": False})
    authed.put("/api/settings", json={"cost_default_per_kg": "25", "cost_margin_pct": "20"})
    quote = authed.post("/api/orders", json={"customer": "Quote Q", "items": [{"model_id": m["id"], "quantity": 1}, {"model_id": m["id"], "quantity": 1, "unit_price": 99}]}).json()
    unknown = _model(authed)
    authed.post(f"/api/orders/{quote['id']}/items", json={"model_id": unknown["id"], "quantity": 1})
    r = authed.post("/api/orders/suggest-prices", json={"margin_pct": 50}).json()
    with Session(engine) as s:
        expected = round(costing.quote(s, 50, 60, costing.defaults_for(s, m["id"])["filament_id"], 1)["unit"]["cost"] * 1.5, 2)
    assert r["filled"] == 1 and r["margin_pct"] == 50 and len(r["skipped"]) == 1 and "no weight or print time" in r["skipped"][0]["why"] and expected > 0
    prices = [i["unit_price"] for i in authed.get(f"/api/orders/{quote['id']}").json()["items"]]
    assert prices == [expected, 99.0, None]
    assert authed.post("/api/orders/suggest-prices", json={}).json()["filled"] == 0                                                   # nothing left to fill
    for bad in ({"margin_pct": -1}, {"margin_pct": "x"}, {"order_ids": "1"}, {"order_ids": [True]}):
        assert authed.post("/api/orders/suggest-prices", json=bad).status_code == 400, bad


def test_the_orders_area_covers_invoices_and_only_the_administrator_voids(authed):
    g = authed.post("/api/access-groups", json={"name": "NoOrders", "deny": ["orders"]}).json()
    authed.post("/api/users", json={"username": "invmember", "password": PW, "role": "member"})
    uid = next(u["id"] for u in authed.get("/api/users").json()["users"] if u["username"] == "invmember")
    auth._login_attempts.clear()
    c = TestClient(_app())
    assert c.post("/api/auth/login", json={"username": "invmember", "password": PW}).status_code == 200
    o = _order(authed, prices=(5.0,), quantity=1)
    inv = _invoice(c, o).json()
    assert c.post(f"/api/invoices/{inv['id']}/void").status_code == 403 and c.get("/api/customers").status_code == 200
    authed.patch(f"/api/users/{uid}", json={"group_id": g["id"]})
    for path in ("/api/invoices", f"/api/invoices/{inv['id']}", "/api/customers"):
        assert c.get(path).status_code == 403, path


# ---------------------------------------------------------------- purchasing
def test_vendors_are_checked_and_listed(authed):
    r = authed.post("/api/purchasing/vendors", json={"name": "Filament Co", "contact": "sales@fc.example", "url": "https://fc.example", "notes": "good PETG"})
    assert r.status_code == 200 and r.json()["name"] == "Filament Co"
    vid = r.json()["id"]
    assert authed.post("/api/purchasing/vendors", json={"name": "Filament Co"}).status_code == 409
    for bad in ({"name": ""}, {"name": "V", "url": "javascript:alert(1)"}, {"name": "V", "url": "ftp://x"}, {"name": 5}, {"name": "V", "contact": 5}):
        assert authed.post("/api/purchasing/vendors", json=bad).status_code == 400, bad
    other = authed.post("/api/purchasing/vendors", json={"name": "Other"}).json()
    assert authed.patch(f"/api/purchasing/vendors/{other['id']}", json={"name": "Filament Co"}).status_code == 409
    assert authed.patch(f"/api/purchasing/vendors/{vid}", json={"notes": "very good"}).json()["notes"] == "very good"
    assert [v["name"] for v in authed.get("/api/purchasing/vendors").json()["vendors"]] == ["Filament Co", "Other"]
    po = authed.post("/api/purchasing/orders", json={"vendor_id": vid}).json()
    authed.delete(f"/api/purchasing/vendors/{vid}")
    assert authed.get(f"/api/purchasing/orders/{po['id']}").json()["vendor_id"] is None                                              # an order does not follow a later vendor
    assert authed.delete(f"/api/purchasing/vendors/{vid}").status_code == 404


def test_a_purchase_order_is_received_once_and_adds_spools_and_parts(authed):
    part = authed.post("/api/inventory", json={"name": "M3 screws", "category": "parts", "quantity": 10, "min_quantity": 5}).json()
    vendor = authed.post("/api/purchasing/vendors", json={"name": "Shop"}).json()
    r = authed.post("/api/purchasing/orders", json={"vendor_id": vendor["id"], "reference": "A-77", "expected_on": "2026-11-01", "lines": [
        {"kind": "spool", "material": "PETG", "brand": "Acme", "color": "Blue", "color_hex": "0000ff", "quantity": 3, "unit_cost": 19.5, "weight_g": 750},
        {"kind": "part", "inventory_id": part["id"], "quantity": 100, "unit_cost": 0.04}]})
    assert r.status_code == 200, r.text
    po = r.json()
    assert po["status"] == "draft" and po["total"] == 62.5 and po["vendor"] == "Shop" and po["lines"][0]["color_hex"] == "#0000FF" and po["lines"][0]["description"] == "Acme PETG Blue" and po["lines"][1]["description"] == "M3 screws"
    for bad in ({"kind": "fish"}, {"kind": "spool"}, {"kind": "spool", "material": "PLA", "quantity": 0}, {"kind": "spool", "material": "PLA", "quantity": 500}, {"kind": "spool", "material": "PLA", "color_hex": "blue"},
                {"kind": "spool", "material": "PLA", "unit_cost": -1}, {"kind": "spool", "material": "PLA", "weight_g": 1}, {"kind": "part", "inventory_id": 999999}, {"kind": "part"}):
        assert authed.post(f"/api/purchasing/orders/{po['id']}/lines", json=bad).status_code == 400, bad
    assert authed.post("/api/purchasing/orders", json={"lines": "x"}).status_code == 400 and authed.post("/api/purchasing/orders", json={"vendor_id": 999999}).status_code == 400
    assert authed.post("/api/purchasing/orders", json={"expected_on": "soon"}).status_code == 400
    empty = authed.post("/api/purchasing/orders", json={}).json()
    assert authed.post(f"/api/purchasing/orders/{empty['id']}/status", json={"status": "ordered"}).status_code == 400
    assert authed.post(f"/api/purchasing/orders/{po['id']}/status", json={"status": "received"}).status_code == 400
    ordered = authed.post(f"/api/purchasing/orders/{po['id']}/status", json={"status": "ordered"}).json()
    assert ordered["status"] == "ordered" and ordered["ordered_on"] == date.today().isoformat()
    before = len(authed.get("/api/filament").json())
    got = authed.post(f"/api/purchasing/orders/{po['id']}/receive").json()
    assert got["spools"] == 3 and got["parts"] == 100 and got["order"]["status"] == "received" and got["order"]["received_on"] == date.today().isoformat()
    spools = authed.get("/api/filament").json()
    new = [s for s in spools if (s.get("notes") or "").startswith(f"From purchase order {po['id']}")]
    assert len(spools) == before + 3 and len(new) == 3 and all(s["material"] == "PETG" and s["spool_weight_g"] == 750 and s["remaining_g"] == 750 and s["cost"] == 19.5 and s["color_hex"] == "#0000FF" for s in new)
    with Session(engine) as s:
        assert s.get(InventoryItem, part["id"]).quantity == 110 and s.get(InventoryItem, part["id"]).unit_cost == 0.04
    assert authed.post(f"/api/purchasing/orders/{po['id']}/receive").status_code == 409                                              # once only
    assert authed.delete(f"/api/purchasing/orders/{po['id']}").status_code == 409 and authed.patch(f"/api/purchasing/orders/{po['id']}", json={"notes": "x"}).status_code == 409
    assert authed.post(f"/api/purchasing/orders/{po['id']}/lines", json={"kind": "spool", "material": "PLA"}).status_code == 409
    cancelled = authed.post("/api/purchasing/orders", json={"lines": [{"kind": "spool", "material": "PLA"}]}).json()
    authed.post(f"/api/purchasing/orders/{cancelled['id']}/status", json={"status": "cancelled"})
    assert authed.post(f"/api/purchasing/orders/{cancelled['id']}/receive").status_code == 409
    assert authed.delete(f"/api/purchasing/orders/{empty['id']}").json() == {"status": "deleted"} and authed.get(f"/api/purchasing/orders/{empty['id']}").status_code == 404
    assert [o["id"] for o in authed.get("/api/purchasing/orders", params={"status": "received"}).json()["orders"]] == [po["id"]] and authed.get("/api/purchasing/orders", params={"status": "x"}).status_code == 400
    for s in new:
        authed.delete(f"/api/filament/{s['id']}")
    authed.delete(f"/api/inventory/{part['id']}")


def test_a_line_can_be_taken_off_a_draft(authed):
    po = authed.post("/api/purchasing/orders", json={"lines": [{"kind": "spool", "material": "PLA", "quantity": 2}, {"kind": "spool", "material": "ABS"}]}).json()
    line = po["lines"][0]["id"]
    assert len(authed.delete(f"/api/purchasing/orders/{po['id']}/lines/{line}").json()["lines"]) == 1
    assert authed.delete(f"/api/purchasing/orders/{po['id']}/lines/{line}").status_code == 404 and authed.delete(f"/api/purchasing/orders/{po['id']}/lines/999999").status_code == 404


def test_the_buy_list_finds_low_spools_queue_demand_and_low_supplies(authed):
    authed.put("/api/settings", json={"low_filament_g": "150"})
    low = authed.post("/api/filament", json={"material": "PLA", "brand": "Zeta", "color": "Red", "spool_weight_g": 1000, "remaining_g": 80}).json()
    fine = authed.post("/api/filament", json={"material": "PETG", "brand": "Zeta", "color": "Green", "spool_weight_g": 1000, "remaining_g": 900}).json()
    short = authed.post("/api/filament", json={"material": "ABS", "brand": "Zeta", "color": "Black", "spool_weight_g": 1000, "remaining_g": 400}).json()
    m = _model(authed)
    authed.post("/api/queue", json={"model_id": m["id"], "filament_id": short["id"], "estimated_grams": 1500})
    part = authed.post("/api/inventory", json={"name": "Fuses", "category": "electronics", "quantity": 1, "min_quantity": 4}).json()
    fine_part = authed.post("/api/inventory", json={"name": "Nuts", "category": "parts", "quantity": 50, "min_quantity": 4}).json()
    try:
        items = authed.get("/api/purchasing/buy-list").json()["items"]
        by = {i["description"]: i for i in items}
        assert by["Zeta PLA Red"]["kind"] == "spool" and by["Zeta PLA Red"]["suggested"] == 1 and "80 g" in by["Zeta PLA Red"]["reason"] and by["Zeta PLA Red"]["weight_g"] == 1000
        assert by["Zeta ABS Black"]["suggested"] == 2 and "needs about 1500 g" in by["Zeta ABS Black"]["reason"]                       # 1100 g short: two spools
        assert by["Fuses"]["kind"] == "part" and by["Fuses"]["suggested"] == 7 and by["Fuses"]["inventory_id"] == part["id"]
        assert "Zeta PETG Green" not in by and "Nuts" not in by
        r = authed.post("/api/purchasing/buy-list/order", json={"items": [by["Zeta PLA Red"], {**by["Fuses"], "quantity": 10}]})
        assert r.status_code == 200, r.text
        po = r.json()
        assert po["status"] == "draft" and po["notes"] == "From the buy list" and [l["quantity"] for l in po["lines"]] == [1, 10] and po["lines"][0]["material"] == "PLA"
        assert authed.post("/api/purchasing/buy-list/order", json={"items": []}).status_code == 400 and authed.post("/api/purchasing/buy-list/order", json={"items": ["x"]}).status_code == 400
    finally:
        for f in (low, fine, short):
            authed.delete(f"/api/filament/{f['id']}")
        for p in (part, fine_part):
            authed.delete(f"/api/inventory/{p['id']}")
        for q in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{q['id']}")


def test_purchasing_counts_as_money_for_access_groups_and_the_page_has_the_controls():
    from pathlib import Path
    assert "/api/purchasing" in auth._AREA_PREFIXES["money"] and "/api/invoices" in auth._AREA_PREFIXES["orders"]
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html, js = (root / "index.html").read_text(encoding="utf-8"), (root / "app.js").read_text(encoding="utf-8")
    for element in ("invoice-settings", "purchasing-box", "customers-box"):
        assert f'id="{element}"' in html, element
    for needle in ("/api/invoices/from-order/", "/payments", "/api/customers", "/api/orders/suggest-prices", "/api/purchasing/orders", "/api/purchasing/buy-list", "/pdf", "invoice_prefix", "tax_rate_pct"):
        assert needle in js or needle in html, needle
