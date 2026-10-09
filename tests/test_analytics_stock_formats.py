"""The analytics report, more kinds of maintenance trigger, Discord-friendly notifications, finished-part stock, stored files, and tougher mesh reading."""
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session
from starlette.testclient import TestClient

from app import maintenance, notify, printers as printing, printwatch, stock
from app.db import engine
from app.models import Filament, MaintenanceTask, Model3D, Order, OrderItem, PrintLog, Printer, QueueItem

PW = "a long enough password"


def _stl(n=None):
    n = n or 30 + uuid.uuid4().int % 300
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 5}\n endloop\nendfacet\nendsolid t\n").encode() + uuid.uuid4().hex.encode()


def _model(c, name=None):
    r = c.post("/api/library/import", files={"file": (name or f"as_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def _printer(c, name="Rep", **extra):
    return c.post("/api/printers", json={"name": name, "kind": "moonraker", "url": "http://klipper.local:7125", **extra}).json()


@pytest.fixture()
def tidy(authed):
    yield
    printwatch.reset()
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")
    for f in authed.get("/api/filament").json():
        authed.delete(f"/api/filament/{f['id']}")
    for s in authed.get("/api/stock").json()["items"]:
        authed.delete(f"/api/stock/{s['id']}")
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")
    authed.put("/api/settings", json={"notify_webhook_url": "", "notify_text_print_done": "", "notify_print_started": "", "notify_print_progress": "", "notify_print_paused": "",
                                      "notify_progress_step": "", "notify_snapshots": "", "hms_tasks": "", "cost_kwh_price": "", "cost_machine_per_hour": "",
                                      "calendar_hours_per_day": ""})


def _log(session, model_id, printer_id, when, minutes, grams, filament_id=None, failed=False, queue_item_id=None, reason=None):
    session.add(PrintLog(model_id=model_id, printer_id=printer_id, printed_at=when, minutes=minutes, grams=grams, filament_id=filament_id, queue_item_id=queue_item_id,
                         outcome="failed" if failed else None, failure_reason=reason if failed else None))


# ---------------------------------------------------------------- analytics
def test_the_report_adds_up_jobs_success_time_filament_and_money(authed, tidy):
    m, a, b = _model(authed), _printer(authed, "Alpha"), _printer(authed, "Bravo")
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "red", "spool_weight_g": 1000, "remaining_g": 900, "cost": 20}).json()
    authed.put("/api/settings", json={"calendar_hours_per_day": "10"})
    day = datetime(2026, 9, 10, 12, 0)
    try:
        with Session(engine) as s:
            _log(s, m["id"], a["id"], day, 60, 100, spool["id"])
            _log(s, m["id"], a["id"], day + timedelta(days=1), 30, 20, spool["id"], failed=True, reason="spaghetti")
            _log(s, m["id"], b["id"], day + timedelta(days=2), 120, 50, spool["id"])
            _log(s, m["id"], None, day + timedelta(days=60), 10, 1, spool["id"])                  # outside the range
            s.commit()
        r = authed.get("/api/analytics", params={"start": "2026-09-01", "end": "2026-09-30", "printer_id": "", "model_id": m["id"]}).json() if False else \
            authed.get("/api/analytics", params={"start": "2026-09-01", "end": "2026-09-30", "model_id": m["id"]}).json()
        c = r["cards"]
        assert (c["jobs"], c["ok"], c["failed"], c["success_rate"]) == (3, 2, 1, 66.7)
        assert c["hours"] == 3.5 and c["grams"] == 170.0 and c["average_minutes"] == 70.0 and c["prints_per_day"] == 0.1 and c["printer_run_hours"] == 3.5
        assert c["cost"] == 3.4                                           # 100 g + 20 g (a failure costs its filament) + 50 g at 20 per kg
        assert r["days"] == 30 and len(r["per_day"]) == 30 and next(d for d in r["per_day"] if d["date"] == "2026-09-11")["failed"] == 1
        alpha = next(p for p in r["by_printer"] if p["printer"] == "Alpha")
        assert (alpha["jobs"], alpha["failed"], alpha["success_rate"], alpha["hours"], alpha["utilisation"]) == (2, 1, 50.0, 1.5, 0.5)       # 1.5 h of 30 days x 10 h
        assert [m_["material"] for m_ in r["by_material"]] == ["PLA"] and r["by_material"][0]["grams"] == 170.0
        assert r["failure_reasons"][0]["count"] == 1 and r["by_model"][0]["jobs"] == 3
        only = authed.get("/api/analytics", params={"start": "2026-09-01", "end": "2026-09-30", "printer_id": b["id"], "model_id": m["id"]}).json()
        assert only["cards"]["jobs"] == 1 and only["cards"]["success_rate"] == 100.0
        none = authed.get("/api/analytics", params={"start": "2026-09-01", "end": "2026-09-30", "material": "PETG", "model_id": m["id"]}).json()
        assert none["cards"]["jobs"] == 0 and none["cards"]["success_rate"] is None and none["cards"]["average_minutes"] is None
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_electricity_and_machine_time_count_for_finished_prints_only(authed, tidy):
    m, a = _model(authed), _printer(authed, "Watt")
    authed.put("/api/settings", json={"cost_kwh_price": "0.5", "cost_printer_watts": "200", "cost_machine_per_hour": "1"})
    try:
        with Session(engine) as s:
            _log(s, m["id"], a["id"], datetime(2026, 9, 5, 8, 0), 120, None)
            _log(s, m["id"], a["id"], datetime(2026, 9, 6, 8, 0), 120, None, failed=True)
            s.commit()
        r = authed.get("/api/analytics", params={"start": "2026-09-01", "end": "2026-09-30", "model_id": m["id"]}).json()
        assert r["cards"]["cost"] == 2.2                                    # 2 h: 0.2 kW x 2 h x 0.5 = 0.20, plus 2 h of machine time = 2.00; the failure counts only filament (none)
    finally:
        authed.put("/api/settings", json={"cost_printer_watts": ""})
        authed.delete(f"/api/library/models/{m['id']}")


def test_an_orders_customer_gets_the_cost_of_its_prints(authed, tidy):
    m = _model(authed)
    spool = authed.post("/api/filament", json={"material": "PETG", "color": "blue", "spool_weight_g": 1000, "remaining_g": 900, "cost": 30}).json()
    order = authed.post("/api/orders", json={"customer": "Report Buyer", "items": [{"model_id": m["id"], "quantity": 1}]}).json()
    try:
        authed.post(f"/api/orders/{order['id']}/queue", json={})
        q = next(x for x in authed.get("/api/queue").json() if x["order_id"] == order["id"])
        with Session(engine) as s:
            _log(s, m["id"], None, datetime(2026, 9, 7, 9, 0), 60, 100, spool["id"], queue_item_id=q["id"])
            s.commit()
        r = authed.get("/api/analytics", params={"start": "2026-09-01", "end": "2026-09-30", "model_id": m["id"]}).json()
        assert r["by_customer"] == [{"customer": "Report Buyer", "jobs": 1, "ok": 1, "failed": 0, "hours": 1.0, "grams": 100.0, "cost": 3.0, "success_rate": 100.0}]
    finally:
        authed.delete(f"/api/orders/{order['id']}")
        authed.delete(f"/api/library/models/{m['id']}")


def test_bad_dates_and_ranges_are_refused_and_the_default_is_30_days(authed, tidy):
    for bad in ({"start": "yesterday"}, {"end": "2026-13-45"}, {"start": "2026-10-10", "end": "2026-10-01"}, {"start": "2000-01-01", "end": "2026-01-01"}):
        assert authed.get("/api/analytics", params=bad).status_code == 400, bad
    r = authed.get("/api/analytics").json()
    assert r["days"] == 30 and len(r["per_day"]) == 30


# ---------------------------------------------------------------- maintenance triggers
def _task(c, printer, **fields):
    r = c.post("/api/maintenance", json={"printer_id": printer["id"], "name": fields.pop("name", "Check it"), **fields})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_task_can_be_due_by_grams_prints_or_failures_and_predicts_its_date(authed, tidy):
    m, p = _model(authed), _printer(authed, "Trig")
    try:
        assert authed.post("/api/maintenance", json={"printer_id": p["id"], "name": "x"}).status_code == 400
        assert authed.post("/api/maintenance", json={"printer_id": p["id"], "name": "x", "every_failures": 2, "failure_reason": "nonsense"}).status_code == 400
        grams, prints = _task(authed, p, name="By grams", every_grams=1000), _task(authed, p, name="By prints", every_prints=10)
        fails = _task(authed, p, name="Clog", every_failures=2, failure_reason="spaghetti")
        now = datetime.utcnow()
        with Session(engine) as s:
            for row in s.query(MaintenanceTask).filter(MaintenanceTask.printer_id == p["id"]).all():
                row.last_done_at = now - timedelta(days=40)                                                                  # done a while ago, so the prints below count
                s.add(row)
            for i in range(3):                                   # 3 finished prints of 100 g, spread over the last 30 days
                _log(s, m["id"], p["id"], now - timedelta(days=i * 5 + 1), 60, 100)
            _log(s, m["id"], p["id"], now - timedelta(days=2), 20, 5, failed=True, reason="spaghetti")
            _log(s, m["id"], p["id"], now - timedelta(days=3), 20, 5, failed=True, reason="clog")
            s.commit()
        rows = {t["name"]: t for t in authed.get("/api/maintenance").json()["tasks"]}
        g = rows["By grams"]
        assert g["grams_since"] == 310.0 and g["status"] == "ok" and g["share"] == 0.31
        assert g["predicted_due"] and g["predicted_due"] > now.date().isoformat()                                     # at the recent rate it is a few weeks away
        assert rows["By prints"]["prints_since"] == 3 and rows["Clog"]["failures_since"] == 1                          # only the spaghetti failure counts
        authed.patch(f"/api/maintenance/{grams['id']}", json={"every_grams": 300})
        assert next(t for t in authed.get("/api/maintenance").json()["tasks"] if t["name"] == "By grams")["status"] == "due"
        assert authed.patch(f"/api/maintenance/{prints['id']}", json={"every_prints": None}).status_code == 400            # a task needs some trigger left
        done = authed.post(f"/api/maintenance/{grams['id']}/done").json()
        assert done["grams_since"] == 0 and done["status"] == "ok"
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_reported_problem_makes_a_task_due_until_it_is_done(authed, tidy):
    p = _printer(authed, "Rattle")
    t = _task(authed, p, name="Check belts", every_days=90)
    assert t["status"] == "ok" and t["flagged"] is False
    r = authed.post(f"/api/maintenance/{t['id']}/report", json={"note": "  rattles on fast moves  "}).json()
    assert r["status"] == "due" and r["flagged"] is True and r["flagged_note"] == "rattles on fast moves" and r["predicted_due"] is None
    assert authed.post(f"/api/maintenance/{t['id']}/report", json={"note": 4}).status_code == 400
    assert authed.post("/api/maintenance/999999/report", json={}).status_code == 404
    done = authed.post(f"/api/maintenance/{t['id']}/done").json()
    assert done["status"] == "ok" and done["flagged"] is False


def test_bambu_fault_codes_are_formatted_and_can_flag_a_task(authed, tidy, monkeypatch):
    assert printing._bambu_hms({"hms": [{"attr": 0x03000100, "code": 0x00010001}, {"attr": "x", "code": 1}, "junk"]}) == ["HMS_0300_0100_0001_0001"]
    p = _printer(authed, "Bambino")
    t = _task(authed, p, name="Replace the nozzle", every_days=365)
    assert authed.put("/api/maintenance/hms-map", json={"map": {"nope": "x"}}).status_code == 400
    assert authed.put("/api/maintenance/hms-map", json={"map": {"HMS_0300_0100_0001_0001": ""}}).status_code == 400
    assert authed.put("/api/maintenance/hms-map", json={"map": {"hms_0300_0100_0001_0001": "Replace the nozzle", "HMS_0500_0200_0001_0002": "Missing task"}}).json()["map"] == \
        {"HMS_0300_0100_0001_0001": "Replace the nozzle", "HMS_0500_0200_0001_0002": "Missing task"}
    assert authed.get("/api/maintenance/hms-map").json()["map"]["HMS_0300_0100_0001_0001"] == "Replace the nozzle"
    told = []
    monkeypatch.setattr(maintenance, "notify_event", lambda session, event, title, message: told.append(message))
    printwatch.latest[p["id"]] = {"online": True, "state": "printing", "name": "Bambino", "hms": ["HMS_0300_0100_0001_0001", "HMS_0500_0200_0001_0002", "HMS_9999_0000_0000_0000"]}
    with Session(engine) as s:
        assert maintenance.apply_hms(s) == ["Bambino: Replace the nozzle"]
        assert maintenance.apply_hms(s) == []                                                                          # already flagged
        assert maintenance.check_and_notify(s) == []                                                                   # and not announced a second time
    task = next(x for x in authed.get("/api/maintenance").json()["tasks"] if x["id"] == t["id"])
    assert task["status"] == "due" and task["flagged_note"] == "Bambu reported HMS_0300_0100_0001_0001" and told == ["Bambino: Replace the nozzle"]


# ---------------------------------------------------------------- notifications
@pytest.fixture()
def hook(authed, monkeypatch):
    posts = []

    def fake_post(url, **kw):
        posts.append((url, kw))
        return httpx.Response(204)
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    return posts


def test_a_discord_webhook_gets_the_picture_attached_and_others_do_not(authed, tidy, hook):
    discord = "https://discord.com/api/webhooks/123/abc"
    authed.put("/api/settings", json={"notify_webhook_url": discord})
    with Session(engine) as s:
        assert notify.notify_event(s, "print_done", "Model Hub: Rep finished", "benchy is done", image=b"\xff\xd8jpeg") is True
    url, kw = hook[-1]
    assert url == discord and "files" in kw and kw["files"]["files[0]"][1] == b"\xff\xd8jpeg"
    assert "benchy is done" in json.loads(kw["data"]["payload_json"])["content"]
    authed.put("/api/settings", json={"notify_webhook_url": "https://ntfy.sh/mytopic"})
    with Session(engine) as s:
        notify.notify_event(s, "print_done", "t", "m", image=b"jpeg")
    assert "files" not in hook[-1][1] and hook[-1][1]["json"]["message"] == "m"                                         # an ordinary webhook: the usual JSON
    assert notify.is_discord("https://canary.discord.com/api/webhooks/1/x") and not notify.is_discord("https://example.com/discord.com") and not notify.is_discord(None)


def test_your_own_wording_fills_in_only_the_known_words(authed, tidy, hook):
    authed.put("/api/settings", json={"notify_webhook_url": "https://ntfy.sh/t", "notify_text_print_done": "{printer} made {file} in {minutes} min {nothing} {__class__}"})
    with Session(engine) as s:
        notify.notify_event(s, "print_done", "t", "default", fields={"printer": "Voron", "file": "gear.stl", "minutes": "42"})
        assert notify.apply_template(s, "print_done", "default", None) == "default"                                    # no fields: the default text
    assert hook[-1][1]["json"]["message"] == "Voron made gear.stl in 42 min {nothing} {__class__}"


def test_the_chatty_events_are_off_until_switched_on(authed, tidy, hook):
    authed.put("/api/settings", json={"notify_webhook_url": "https://ntfy.sh/t"})
    events = {e["id"]: e for e in authed.get("/api/settings/notify-events").json()["events"]}
    assert events["print_started"]["enabled"] is False and events["print_progress"]["enabled"] is False and events["print_done"]["enabled"] is True
    assert "stock_low" in events and authed.get("/api/settings/notify-events").json()["placeholders"] == ["printer", "file", "progress", "minutes"]
    with Session(engine) as s:
        assert notify.notify_event(s, "print_started", "t", "m") is False
    authed.put("/api/settings", json={"notify_print_started": "true"})
    with Session(engine) as s:
        assert notify.notify_event(s, "print_started", "t", "m") is True


def test_a_print_that_starts_pauses_and_passes_steps_is_announced_once_each(authed, tidy, hook, monkeypatch):
    authed.put("/api/settings", json={"notify_webhook_url": "https://discord.com/api/webhooks/1/x", "notify_print_started": "true", "notify_print_progress": "true",
                                      "notify_print_paused": "true", "notify_progress_step": "25"})
    p = _printer(authed, "Announcer", snapshot_url="http://cam.local/s")
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: b"jpeg")
    printwatch.reset()

    def poll(state, progress):
        with Session(engine) as s:
            printer = s.get(Printer, p["id"])
            previous = printwatch._last.get(printer.id)
            st = {"online": True, "state": state, "progress": progress, "file": "gear.gcode"}
            printwatch._announce(s, printer, previous, st)
            printwatch._last[printer.id] = {"state": state, "file": "gear.gcode", "progress": progress}

    def said():
        out = []
        for _, kw in hook:
            payload = kw["data"]["payload_json"] if "data" in kw else json.dumps(kw["json"])
            out.append(json.loads(payload)["content"])
        return out
    poll("idle", None)
    poll("printing", 0)                                                                                                    # started
    poll("printing", 10)
    poll("printing", 30)                                                                                                   # passed 25
    poll("printing", 40)
    poll("printing", 55)                                                                                                   # passed 50
    poll("paused", 56)
    poll("printing", 57)                                                                                                   # resumed
    poll("printing", 80)                                                                                                   # passed 75
    poll("printing", 100)                                                                                                  # 100 is the finish's own message
    texts = " | ".join(said())
    assert texts.count("Announcer started") == 1 and texts.count("25%") >= 1 and texts.count("50%") >= 1 and texts.count("75%") >= 1 and "100%" not in texts.replace("is 100", "")
    assert "paused" in texts and "printing again" in texts
    assert all("files" in kw for _, kw in hook)                                                                             # Discord: each carried the camera picture


def test_nothing_is_announced_on_the_first_look_mid_print(authed, tidy, hook):
    authed.put("/api/settings", json={"notify_webhook_url": "https://ntfy.sh/t", "notify_print_started": "true", "notify_print_progress": "true"})
    p = _printer(authed, "Restart")
    printwatch.reset()
    with Session(engine) as s:
        printwatch._announce(s, s.get(Printer, p["id"]), None, {"online": True, "state": "printing", "progress": 60, "file": "x"})
    assert hook == []                                                                                                       # Model Hub was restarted mid-print: nothing is guessed


# ---------------------------------------------------------------- stock
def test_stock_is_checked_adjusted_and_sorted_out_when_a_model_goes(authed, tidy):
    m = _model(authed)
    try:
        assert authed.post("/api/stock", json={"model_id": 987654}).status_code == 400
        assert authed.post("/api/stock", json={"model_id": m["id"], "on_hand": -1}).status_code == 400
        item = authed.post("/api/stock", json={"model_id": m["id"], "on_hand": 5, "minimum": 3, "sku": " HOOK-1 "}).json()
        assert item["on_hand"] == 5 and item["minimum"] == 3 and item["sku"] == "HOOK-1" and item["low"] is False and item["short"] == 0
        assert authed.post("/api/stock", json={"model_id": m["id"]}).status_code == 400                                       # one entry per model
        assert authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": -3}).json()["low"] is True
        assert authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": -9}).status_code == 400
        assert authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": 0}).status_code == 400
        assert authed.patch(f"/api/stock/{item['id']}", json={"minimum": 4}).json()["short"] == 2
        listed = authed.get("/api/stock").json()
        assert listed["on_hand"] == 2 and listed["low"] == 1 and listed["items"][0]["filename"] == m["filename"]
    finally:
        authed.delete(f"/api/library/models/{m['id']}")
    assert authed.get("/api/stock").json()["items"] == []                                                                       # the shelf entry went with its model


def test_making_more_queues_the_shortfall_and_each_finished_print_goes_on_the_shelf_once(authed, tidy):
    m = _model(authed)
    item = authed.post("/api/stock", json={"model_id": m["id"], "on_hand": 1, "minimum": 4}).json()
    try:
        made = authed.post(f"/api/stock/{item['id']}/make", json={}).json()
        assert made["queued"] == 3 and made["item"]["queued"] == 3 and made["item"]["short"] == 0                            # 4 wanted - 1 on hand
        assert authed.post(f"/api/stock/{item['id']}/make", json={}).json()["queued"] == 0                                    # already covered
        more = authed.post(f"/api/stock/{item['id']}/make", json={"quantity": 2}).json()
        assert more["queued"] == 2 and authed.post(f"/api/stock/{item['id']}/make", json={"quantity": 0}).status_code == 400
        mine = [q for q in authed.get("/api/queue").json() if q["to_stock_id"] == item["id"]]
        assert len(mine) == 5
        authed.patch(f"/api/queue/{mine[0]['id']}", json={"status": "done"})
        authed.patch(f"/api/queue/{mine[0]['id']}", json={"status": "printing"})
        authed.patch(f"/api/queue/{mine[0]['id']}", json={"status": "done"})                                                  # done twice: one part
        assert authed.get("/api/stock").json()["items"][0]["on_hand"] == 2
        authed.delete(f"/api/stock/{item['id']}")
        assert all(q["to_stock_id"] is None for q in authed.get("/api/queue").json() if q["id"] in {x["id"] for x in mine})
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_an_order_can_take_what_is_on_the_shelf(authed, tidy):
    m = _model(authed)
    item = authed.post("/api/stock", json={"model_id": m["id"], "on_hand": 3}).json()
    order = authed.post("/api/orders", json={"customer": "Shelf Buyer", "items": [{"model_id": m["id"], "quantity": 5}]}).json()
    try:
        authed.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
        r = authed.post(f"/api/orders/{order['id']}/take-from-stock").json()
        assert r["taken"] == 3 and r["order"]["items"][0]["from_stock"] == 3 and r["order"]["items"][0]["units_done"] == 3
        assert authed.get("/api/stock").json()["items"][0]["on_hand"] == 0
        assert authed.post(f"/api/orders/{order['id']}/take-from-stock").json()["taken"] == 0                                 # nothing left to take
        assert authed.post(f"/api/orders/{order['id']}/queue", json={}).json()["queued"] == 2                                 # only the rest is printed
        mine = [q for q in authed.get("/api/queue").json() if q["order_id"] == order["id"]]
        for q in mine:
            authed.patch(f"/api/queue/{q['id']}", json={"status": "done"})
        assert authed.get(f"/api/orders/{order['id']}").json()["status"] == "ready"
    finally:
        authed.delete(f"/api/orders/{order['id']}")
        authed.delete(f"/api/library/models/{m['id']}")


def test_an_order_filled_entirely_from_the_shelf_is_ready_at_once(authed, tidy):
    m = _model(authed)
    authed.post("/api/stock", json={"model_id": m["id"], "on_hand": 9})
    order = authed.post("/api/orders", json={"customer": "Quick Buyer", "items": [{"model_id": m["id"], "quantity": 2}]}).json()
    try:
        authed.patch(f"/api/orders/{order['id']}", json={"status": "accepted"})
        authed.post(f"/api/orders/{order['id']}/take-from-stock")
        assert authed.get(f"/api/orders/{order['id']}").json()["status"] == "ready"
        assert authed.get("/api/stock").json()["items"][0]["on_hand"] == 7
    finally:
        authed.delete(f"/api/orders/{order['id']}")
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_part_running_low_is_announced_once_and_again_after_restocking(authed, tidy, monkeypatch):
    m = _model(authed)
    item = authed.post("/api/stock", json={"model_id": m["id"], "on_hand": 5, "minimum": 3}).json()
    told = []
    monkeypatch.setattr(stock, "notify_event", lambda session, event, title, message: told.append((event, message)))
    try:
        with Session(engine) as s:
            assert stock.check_and_notify(s) == []
        authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": -3})
        with Session(engine) as s:
            assert len(stock.check_and_notify(s)) == 1 and stock.check_and_notify(s) == []
        assert told[0][0] == "stock_low" and "2 left, minimum 3" in told[0][1]
        authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": 10})
        with Session(engine) as s:
            assert stock.check_and_notify(s) == []
        authed.post(f"/api/stock/{item['id']}/adjust", json={"delta": -11})
        with Session(engine) as s:
            assert len(stock.check_and_notify(s)) == 1
        from app import scheduler
        assert "stock" in scheduler._jobs()
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- files that are only kept
def _attach(c, model_id, name="part.f3d", data=b"fusion bytes", notes=None):
    return c.post("/api/attachments", data={"model_id": str(model_id), **({"notes": notes} if notes else {})}, files={"file": (name, data, "application/octet-stream")})


def test_a_cad_project_is_kept_with_the_model_and_labelled_honestly(authed):
    m = _model(authed)
    try:
        r = _attach(authed, m["id"], "Bracket v3.f3d", b"fusion bytes", "the editable source")
        assert r.status_code == 200
        a = r.json()
        assert a["label"] == "Fusion 360" and "stored only" in a["capability"] and a["size_bytes"] == 12 and a["kind"] == "f3d" and a["notes"] == "the editable source" and a["file_exists"]
        assert [x["id"] for x in authed.get("/api/attachments", params={"model_id": m["id"]}).json()] == [a["id"]]
        d = authed.get(f"/api/attachments/{a['id']}/download")
        assert d.content == b"fusion bytes" and "attachment" in d.headers["content-disposition"] and d.headers["x-content-type-options"] == "nosniff"
        changed = authed.patch(f"/api/attachments/{a['id']}", json={"notes": "changed"})
        assert changed.status_code == 200 and "notes" in changed.json(), changed.text
        assert changed.json()["notes"] == "changed"
        assert authed.delete(f"/api/attachments/{a['id']}").status_code == 200 and authed.get(f"/api/attachments/{a['id']}/download").status_code == 404
        unknown = _attach(authed, m["id"], "thing.xyz123", b"?").json()
        assert unknown["label"] == "XYZ123 file"
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_dangerous_and_model_files_are_refused_and_names_are_cleaned(authed):
    m = _model(authed)
    try:
        for name in ("run.exe", "x.sh", "page.html", "a.PS1"):
            assert _attach(authed, m["id"], name).status_code == 400, name
        assert _attach(authed, m["id"], "another.stl").status_code == 400 and "Import" in _attach(authed, m["id"], "another.stl").json()["detail"]
        assert _attach(authed, m["id"], "empty.blend", b"").status_code == 400
        assert _attach(authed, 999999, "x.blend").status_code == 404
        cleaned = _attach(authed, m["id"], "../../etc/passwd.blend").json()
        assert "/" not in cleaned["filename"] and ".." not in cleaned["filename"].replace("passwd.blend", "")
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_deleting_a_model_removes_its_stored_files_and_the_table_says_what_is_opened(authed):
    from app.routers.attachments_router import stored_path
    m = _model(authed)
    a = _attach(authed, m["id"], "scene.blend", b"blender").json()
    from app.models import ModelAttachment
    with Session(engine) as s:
        path = stored_path(s.get(ModelAttachment, a["id"]).stored_name)
    assert path.is_file()
    authed.delete(f"/api/library/models/{m['id']}")
    assert not path.exists() and authed.get(f"/api/attachments/{a['id']}/download").status_code == 404
    table = authed.get("/api/attachments/formats").json()
    opened = {o["extension"]: o for o in table["opened"]}
    assert opened["stl"]["analyse"] is True and opened["step"]["analyse"] is False and opened["step"]["viewer"] is True and "stp" not in opened
    assert {"extension": "blend", "label": "Blender"} in table["stored"] and "exe" in table["refused"]


# ---------------------------------------------------------------- reading tough meshes
CUBE = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
TRIS = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _mesh_xml(scale=10.0, shift=0.0):
    verts = "".join(f'<vertex x="{x * scale + shift}" y="{y * scale}" z="{z * scale}"/>' for x, y, z in CUBE)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in TRIS)
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"


def _project_3mf(path, parts=2):
    """A slicer-project style 3MF: the main model only refers (Production extension) to objects in other model files, with transforms."""
    ns = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
    comps = "".join(f'<component p:path="/3D/Objects/part{i}.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 {i * 30} 0 0"/>' for i in range(parts))
    main = f'<?xml version="1.0"?><model unit="millimeter" {ns}><resources><object id="10" type="model"><components>{comps}</components></object></resources><build><item objectid="10"/></build></model>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", main)
        for i in range(parts):
            z.writestr(f"3D/Objects/part{i}.model", f'<?xml version="1.0"?><model unit="millimeter" {ns}><resources><object id="1" type="model">{_mesh_xml(10.0 + i)}</object></resources><build/></model>')
        z.writestr("Metadata/project_settings.config", "{}")


def test_a_slicer_project_with_parts_in_other_files_is_read_directly(tmp_path):
    from app import threemf_fallback
    path = tmp_path / "project.3mf"
    _project_3mf(path, parts=2)
    mesh = threemf_fallback.load(path)
    assert len(mesh.faces) == 24 and len(mesh.vertices) == 16
    lo, hi = mesh.bounds
    assert lo[0] == pytest.approx(0) and hi[0] == pytest.approx(30 + 11) and hi[1] == pytest.approx(11)                  # the second part is moved 30 mm along x and is 11 mm wide


def test_the_fallback_refuses_what_has_no_mesh_and_survives_damage(tmp_path):
    from app import threemf_fallback
    empty = tmp_path / "empty.3mf"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("3D/3dmodel.model", '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources/><build/></model>')
    broken = tmp_path / "broken.3mf"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("3D/3dmodel.model", "<model><resources>")
    notzip = tmp_path / "notzip.3mf"
    notzip.write_bytes(b"this is not a zip")
    loop = tmp_path / "loop.3mf"
    ns = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
    with zipfile.ZipFile(loop, "w") as z:
        z.writestr("3D/3dmodel.model", f'<model {ns}><resources><object id="1"><components><component objectid="1"/></components></object></resources><build><item objectid="1"/></build></model>')
    for bad in (empty, broken, notzip, loop):
        with pytest.raises(threemf_fallback.ThreeMFError):
            threemf_fallback.load(bad)                                                                                  # a clear error each time, never a crash or a hang


def test_a_project_3mf_imports_and_can_be_checked(authed, tmp_path):
    path = tmp_path / f"proj_{uuid.uuid4().hex[:6]}.3mf"
    _project_3mf(path, parts=3)
    r = authed.post("/api/library/import", files={"file": (path.name, path.read_bytes(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    m = r.json()
    try:
        assert m["extension"] == ".3mf" and (m.get("face_count") or 0) >= 12
        h = authed.get(f"/api/library/models/{m['id']}/health")
        assert h.status_code == 200 and h.json()["faces"] >= 36
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_broken_files_are_indexed_or_refused_but_never_crash_the_server(authed):
    nan = ("solid t\nfacet normal 0 0 1\n outer loop\n vertex nan 0 0\n vertex 1 0 0\n vertex 0 1 0\n endloop\nendfacet\nendsolid t\n" + uuid.uuid4().hex).encode()
    truncated = b"\x00" * 80 + (1000).to_bytes(4, "little") + b"\x00" * 100 + uuid.uuid4().bytes
    for name, data in ((f"empty_{uuid.uuid4().hex[:5]}.stl", b""), (f"nan_{uuid.uuid4().hex[:5]}.stl", nan), (f"cut_{uuid.uuid4().hex[:5]}.stl", truncated),
                       (f"junk_{uuid.uuid4().hex[:5]}.3mf", b"PK\x03\x04 not really"), (f"huge_{uuid.uuid4().hex[:5]}.obj", b"v 1e308 0 0\nv 0 1e308 0\nv 0 0 1e308\nf 1 2 3\n")):
        r = authed.post("/api/library/import", files={"file": (name, data, "application/octet-stream")})
        assert r.status_code < 500, (name, r.status_code, r.text[:200])
        if r.status_code == 200:
            mid = r.json()["id"]
            h = authed.get(f"/api/library/models/{mid}/health")
            assert h.status_code < 500, (name, h.status_code, h.text[:200])
            authed.delete(f"/api/library/models/{mid}")


def test_the_corpus_tool_reports_without_crashing(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("corpus_check", Path(__file__).resolve().parent.parent / "tools" / "corpus_check.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    (tmp_path / "good.stl").write_bytes(_stl())
    (tmp_path / "empty.stl").write_bytes(b"")
    _project_3mf(tmp_path / "sub.3mf")
    (tmp_path / "notes.txt").write_text("ignored")
    report = tmp_path / "report.json"
    assert tool.main([str(tmp_path), "--json", str(report)]) == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert sum(data["summary"].values()) == 3 and data["summary"].get("crashed", 0) == 0 and data["summary"]["checked"] >= 2
    assert any(f["status"] == "unreadable" for f in data["files"])
    assert tool.main([str(tmp_path), "--limit", "1"]) == 0


# ---------------------------------------------------------------- the page
def test_the_controls_are_in_the_page():
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    for element in ("analytics-box", "analytics-start", "analytics-end", "analytics-printer", "stock-box", "notify-progress-step", "notify-snapshots", "hms-map"):
        assert f'id="{element}"' in html, element
    for needle in ("/api/analytics", "/api/stock", "/take-from-stock", "/api/attachments", "attachment-upload", "maint-report", "every_grams", "/api/maintenance/hms-map", "notify-text", "predicted_due"):
        assert needle in js, needle
