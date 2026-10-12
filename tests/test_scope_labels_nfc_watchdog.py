"""Queue entries that belong to someone and wait for approval, printer labels, known-good versions, spool tags, and the dispatch watchdog."""
import json
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, select
from starlette.testclient import TestClient

from app import auth, capabilities, dispatch_watch, nfc, printers as printing, printwatch
from app.db import engine
from app.models import Filament, Model3D, PrinterJob, QueueItem
from test_printers import FakePrinter
from test_print_farm import farm, _keep, _queue  # noqa: F401  (the fixture and helpers)

PW = "a long enough password"


def _app():
    from app.main import app
    return app


def _login(name):
    auth._login_attempts.clear()
    c = TestClient(_app())
    assert c.post("/api/auth/login", json={"username": name, "password": PW}).status_code == 200
    return c


@pytest.fixture(autouse=True)
def _tidy(authed):
    yield
    authed.put("/api/settings", json={"dispatch_watchdog": "", "notify_review_needed": ""})
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")
    for u in authed.get("/api/users").json()["users"]:
        authed.delete(f"/api/users/{u['id']}")
    for g in authed.get("/api/access-groups").json()["groups"]:
        authed.delete(f"/api/access-groups/{g['id']}")
    printwatch.reset()
    dispatch_watch.reset()


def _stl():
    n = 30 + uuid.uuid4().int % 300
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} {n // 5}\n endloop\nendfacet\nendsolid t\n").encode() + uuid.uuid4().hex.encode()


def _model(c):
    r = c.post("/api/library/import", files={"file": (f"sc_{uuid.uuid4().hex[:6]}.stl", _stl(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- labels
def test_labels_are_read_from_text_or_a_dict_and_compared_without_case():
    assert capabilities.clean("Plate=Textured, enclosed = yes") == {"plate": "Textured", "enclosed": "yes"}
    assert capabilities.clean({"Build Plate": "smooth", "heated": True, "n": 0.6}) == {"build_plate": "smooth", "heated": "yes", "n": "0.6"}
    assert capabilities.clean("a b c=1") == {"a_b_c": "1"}                                                                          # spaces in a name become underscores
    assert capabilities.clean("") is None and capabilities.clean(None) is None and capabilities.clean({}) is None
    for bad in ("plate", "plate=", "=x", "plate=a;b!", {"k" * 30: "x"}, {f"k{i}": "x" for i in range(13)}, 5, ["plate=x"], "x=" + "y" * 50, "-a=b"):
        with pytest.raises(capabilities.CapabilityError):
            capabilities.clean(bad)
    assert capabilities.missing({"plate": "Textured"}, {"plate": "textured "}) == [] and capabilities.missing({"plate": "textured", "enclosed": "yes"}, {"plate": "smooth"}) == ["plate: textured", "enclosed: yes"]
    assert capabilities.missing({}, {"x": "y"}) == [] and capabilities.load("junk") == {} and capabilities.load('["a"]') == {} and capabilities.text({"b": "2", "a": "1"}) == "a=1, b=2"
    assert capabilities.dump(None) is None and json.loads(capabilities.dump({"a": "1"})) == {"a": "1"}


def test_a_print_that_needs_a_plate_goes_only_to_a_printer_that_has_it(authed, farm):
    fake, a, b, m, _ = farm
    _keep(authed, m)
    assert authed.patch(f"/api/printers/{a['id']}", json={"capabilities": "plate=textured, enclosed=yes"}).json()["capabilities"] == {"plate": "textured", "enclosed": "yes"}
    assert authed.patch(f"/api/printers/{b['id']}", json={"capabilities": {"plate": "smooth"}}).json()["capabilities"] == {"plate": "smooth"}
    assert authed.patch(f"/api/printers/{a['id']}", json={"capabilities": "oops"}).status_code == 400
    item = _queue(authed, farm, requires="Plate=Textured")
    assert item["requires"] == '{"plate": "Textured"}'
    suggested = authed.get("/api/queue/suggestions").json()[str(item["id"])]
    assert [s["printer_id"] for s in suggested] == [a["id"]]
    none = _queue(authed, farm)
    assert {s["printer_id"] for s in authed.get("/api/queue/suggestions").json()[str(none["id"])]} == {a["id"], b["id"]}                 # no need, any printer
    assert authed.patch(f"/api/queue/{item['id']}", json={"requires": "plate=PEI, enclosed=yes"}).json()["requires"] == '{"enclosed": "yes", "plate": "PEI"}'
    assert authed.patch(f"/api/queue/{item['id']}", json={"requires": "bad"}).status_code == 400
    assert authed.get("/api/queue/suggestions").json().get(str(item["id"])) is None                                                  # nothing has PEI
    authed.patch(f"/api/queue/{item['id']}", json={"requires": "plate=smooth", "printer_id": a["id"]})
    fake.moonraker_state = "standby"
    r = authed.post(f"/api/queue/{item['id']}/send", json={"start": False})
    assert r.status_code == 409 and r.json()["detail"].startswith("Needs: ") and "plate: smooth" in r.json()["detail"]
    assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False, "force": True}).status_code == 409                        # not a shortcut
    authed.patch(f"/api/queue/{item['id']}", json={"printer_id": b["id"]})
    assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200
    assert authed.patch(f"/api/queue/{item['id']}", json={"requires": None}).json()["requires"] is None
    assert authed.post("/api/queue", json={"model_id": m["id"], "requires": "nonsense"}).status_code == 400


# ---------------------------------------------------------------- own entries and approval
def _member(authed, name, group=None):
    uid = authed.post("/api/users", json={"username": name, "password": PW, "role": "member"}).json()["id"]
    if group:
        authed.patch(f"/api/users/{uid}", json={"group_id": group})
    return uid


def test_a_group_can_limit_logins_to_their_own_queue_entries(authed, farm):
    fake, a, b, m, _ = farm
    g = authed.post("/api/access-groups", json={"name": "Students", "own_only": True}).json()
    assert g["own_only"] is True and g["needs_review"] is False
    assert authed.post("/api/access-groups", json={"name": "Bad", "own_only": "yes"}).status_code == 400
    _member(authed, "ann_s", g["id"])
    _member(authed, "bob_s", g["id"])
    _member(authed, "free_s")
    ann, bob, free = _login("ann_s"), _login("bob_s"), _login("free_s")
    mine = ann.post("/api/queue", json={"model_id": m["id"]}).json()
    theirs = bob.post("/api/queue", json={"model_id": m["id"]}).json()
    staff = authed.post("/api/queue", json={"model_id": m["id"]}).json()
    owners = {q["id"]: q["owner"] for q in authed.get("/api/queue").json()}
    assert owners[mine["id"]] == "ann_s" and owners[theirs["id"]] == "bob_s" and owners[staff["id"]] and len(owners) >= 3                      # the administrator sees all
    assert {q["id"] for q in ann.get("/api/queue").json()} == {mine["id"]} and {q["id"] for q in bob.get("/api/queue").json()} == {theirs["id"]}
    assert len(free.get("/api/queue").json()) >= 3                                                                                  # a login with no group sees all
    assert ann.patch(f"/api/queue/{theirs['id']}", json={"notes": "mine now"}).status_code == 403
    assert "belongs to someone else" in ann.delete(f"/api/queue/{theirs['id']}").json()["detail"]
    assert ann.patch(f"/api/queue/{mine['id']}", json={"notes": "fine"}).status_code == 200 and ann.delete(f"/api/queue/{mine['id']}").status_code == 200
    ann.post(f"/api/queue/again/{m['id']}")
    assert all(q["owner"] == "ann_s" for q in ann.get("/api/queue").json())                                                         # what ann queues again is hers
    authed.patch(f"/api/access-groups/{g['id']}", json={"own_only": False})
    assert len(ann.get("/api/queue").json()) >= 3                                                                                   # applies at once


def test_entries_of_a_review_group_wait_until_the_administrator_approves(authed, farm, monkeypatch):
    fake, a, b, m, _ = farm
    told = []
    from app.routers import queue as queue_router
    import app.notify as notify
    monkeypatch.setattr(notify, "notify_event", lambda session, event, title, message, **kw: told.append((event, title, message)))
    g = authed.post("/api/access-groups", json={"name": "Reviewed", "needs_review": True}).json()
    _member(authed, "rev_one", g["id"])
    c = _login("rev_one")
    item = c.post("/api/queue", json={"model_id": m["id"], "printer_id": a["id"]}).json()
    assert item["held"] is True and item["review"] == "pending" and item["owner"] == "rev_one"
    assert len(told) == 1 and told[0][0] == "review_needed" and "rev_one" in told[0][2]
    assert c.patch(f"/api/queue/{item['id']}", json={"held": False}).status_code == 403                                              # no unlocking it themselves
    assert c.post(f"/api/queue/{item['id']}/review", json={"action": "approve"}).status_code == 403
    assert authed.post(f"/api/queue/{item['id']}/review", json={"action": "maybe"}).status_code == 400 and authed.post("/api/queue/999999/review", json={"action": "approve"}).status_code == 404
    assert authed.post(f"/api/queue/{item['id']}/review", json={"action": "approve", "note": 5}).status_code == 400
    _keep(authed, m)
    fake.moonraker_state = "standby"
    assert "hold" in authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).json()["detail"]                              # a held entry is not sent
    sug = authed.get("/api/queue/suggestions").json()
    assert str(item["id"]) not in sug
    rejected = authed.post(f"/api/queue/{item['id']}/review", json={"action": "reject", "note": "too big for the class budget"}).json()
    assert "review" in rejected, rejected
    assert rejected["review"] == "rejected" and rejected["held"] is True and rejected["review_note"] == "too big for the class budget"
    approved = authed.post(f"/api/queue/{item['id']}/review", json={"action": "approve"}).json()
    assert approved["review"] == "approved" and approved["held"] is None and approved["review_note"] is None
    assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200
    plain = authed.post("/api/queue", json={"model_id": m["id"]}).json()
    assert plain["held"] is None and plain["review"] is None and len(told) == 1                                                       # the administrator's own entries are not held
    assert any("Approved the queue entry" in x["summary"] for x in authed.get("/api/activity").json()["items"])


# ---------------------------------------------------------------- known good
def test_a_print_that_worked_marks_a_model_known_good(authed, farm):
    fake, a, b, m, _ = farm
    assert authed.get(f"/api/library/models/{m['id']}").json().get("known_status") is None
    authed.post("/api/prints", json={"model_id": m["id"], "outcome": "failed", "failure_reason": "clog"})
    assert authed.get(f"/api/library/models/{m['id']}").json().get("known_status") is None                                             # a failure says nothing good
    authed.patch(f"/api/library/models/{m['id']}", json={"known_status": "needs_test"})
    authed.post("/api/prints", json={"model_id": m["id"], "minutes": 30})
    assert authed.get(f"/api/library/models/{m['id']}").json()["known_status"] == "known_good"
    authed.patch(f"/api/library/models/{m['id']}", json={"known_status": "archived"})
    authed.post("/api/prints", json={"model_id": m["id"]})
    assert authed.get(f"/api/library/models/{m['id']}").json()["known_status"] == "archived"                                          # a judgement already made is kept
    authed.patch(f"/api/library/models/{m['id']}", json={"known_status": "failed"})
    authed.post("/api/prints", json={"model_id": m["id"]})
    assert authed.get(f"/api/library/models/{m['id']}").json()["known_status"] == "failed"
    assert authed.patch(f"/api/library/models/{m['id']}", json={"known_status": "perfect"}).status_code == 400
    assert authed.patch(f"/api/library/models/{m['id']}", json={"known_status": ""}).json()["known_status"] is None
    for log in authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"]:
        authed.delete(f"/api/prints/{log['id']}")


def test_one_version_per_family_is_the_one_to_print_and_models_can_be_filtered(authed):
    one, two = _model(authed), _model(authed)
    try:
        authed.post("/api/families", json={"model_ids": [one["id"], two["id"]]})
        assert authed.patch(f"/api/library/models/{one['id']}", json={"recommended": True}).json()["recommended"] is True
        assert authed.patch(f"/api/library/models/{two['id']}", json={"recommended": True}).json()["recommended"] is True
        assert authed.get(f"/api/library/models/{one['id']}").json()["recommended"] is None                                            # only one at a time
        assert authed.patch(f"/api/library/models/{two['id']}", json={"recommended": "yes"}).status_code == 400
        authed.patch(f"/api/library/models/{one['id']}", json={"known_status": "known_good"})
        authed.patch(f"/api/library/models/{two['id']}", json={"known_status": "needs_test"})
        ids = lambda **q: {x["id"] for x in authed.get("/api/library/models", params={"limit": 5000, **q}).json()}
        assert one["id"] in ids(known="known_good") and two["id"] not in ids(known="known_good") and two["id"] in ids(known="needs_test")
        assert {one["id"], two["id"]} <= ids(known="any") and one["id"] not in ids(known="none")
        assert authed.get("/api/library/models", params={"known": "great"}).status_code == 400
    finally:
        for m in (one, two):
            authed.delete(f"/api/library/models/{m['id']}")


# ---------------------------------------------------------------- spool tags
def test_what_a_tag_says_is_understood_carefully():
    assert nfc.parse("https://hub.local:8420/#/spool/12") == {"spool_id": 12} and nfc.parse("SPOOL:7") == {"spool_id": 7} and nfc.parse(" spool:7 ") == {"spool_id": 7}
    open_spool = json.dumps({"protocol": "openspool", "version": "1.0", "type": "PETG", "color_hex": "ff8800", "brand": "Generic", "min_temp": 220, "max_temp": 250})
    assert nfc.parse(open_spool) == {"spec": {"material": "PETG", "brand": "Generic", "color_hex": "#FF8800", "min_temp": 220, "max_temp": 250}}
    odd = json.dumps({"protocol": "OpenSpool", "type": "PLA", "color_hex": "nothex", "min_temp": True, "max_temp": 9999})
    assert nfc.parse(odd) == {"spec": {"material": "PLA", "brand": None, "color_hex": None}}
    for nothing in ("", "   ", None, 5, "hello", "{not json", json.dumps({"protocol": "other"}), json.dumps([1]), "SPOOL:abc", "#/spool/"):
        assert nfc.parse(nothing) == {}, nothing
    assert nfc.parse("x" * 10000 + " SPOOL:1") == {}                                                                                  # only the start of a huge text is read


def test_a_tag_is_linked_to_one_spool_at_a_time_and_can_make_a_spool(authed):
    one = authed.post("/api/filament", json={"material": "PLA", "brand": "A", "color": "red"}).json()
    two = authed.post("/api/filament", json={"material": "PETG", "brand": "B", "color": "blue"}).json()
    try:
        assert authed.post("/api/nfc/lookup", json={"uid": "04:A1:B2:C3"}).json() == {"match": None, "how": None, "parsed": {}}
        r = authed.post("/api/nfc/claim", json={"uid": "04:a1:b2:c3", "filament_id": one["id"]}).json()
        assert r["spool"]["nfc_tag"] == "04:A1:B2:C3" and r["moved_from"] is None
        found = authed.post("/api/nfc/lookup", json={"uid": "04:A1:B2:C3"}).json()
        assert found["match"]["id"] == one["id"] and found["how"] == "the tag number"
        moved = authed.post("/api/nfc/claim", json={"uid": "04:A1:B2:C3", "filament_id": two["id"]}).json()
        assert moved["moved_from"] == one["id"] and authed.post("/api/nfc/lookup", json={"uid": "04:A1:B2:C3"}).json()["match"]["id"] == two["id"]
        by_number = authed.post("/api/nfc/lookup", json={"payload": f"SPOOL:{one['id']}"}).json()
        assert by_number["match"]["id"] == one["id"] and by_number["how"] == "the spool number on the tag" and by_number["parsed"] == {"spool_id": one["id"]}
        assert authed.post("/api/nfc/lookup", json={"payload": "SPOOL:999999"}).json()["match"] is None
        assert authed.post("/api/nfc/claim", json={"uid": "bad", "filament_id": one["id"]}).status_code == 400 and authed.post("/api/nfc/claim", json={"uid": "04A1B2C3", "filament_id": 999999}).status_code == 400
        assert authed.post("/api/nfc/claim", json={"uid": "04A1B2C3", "filament_id": True}).status_code == 400 and authed.post("/api/nfc/lookup", json={"uid": 5}).status_code == 400
        spec = nfc.parse(json.dumps({"protocol": "openspool", "type": "ABS", "color_hex": "112233", "brand": "Made"}))["spec"]
        made = authed.post("/api/nfc/create", json={"spec": spec, "uid": "04DEADBEEF"}).json()
        assert made["material"] == "ABS" and made["brand"] == "Made" and made["color_hex"] == "#112233" and made["nfc_tag"] == "04DEADBEEF"
        assert authed.post("/api/nfc/create", json={"spec": {"material": ""}}).status_code == 400 and authed.post("/api/nfc/create", json={}).status_code == 400
        authed.delete(f"/api/filament/{made['id']}")
    finally:
        for f in (one, two):
            authed.delete(f"/api/filament/{f['id']}")


class Spoolman(FakePrinter):
    def __init__(self):
        super().__init__()
        self.told = []

    def __call__(self, request):
        if request.method == "POST" and request.url.path == "/server/spoolman/spool_id":
            self.told.append(json.loads(request.content))
            return httpx.Response(200, json={"result": {"spool_id": 1}})
        return super().__call__(request)


def test_klipper_is_told_which_spoolman_spool_is_loaded(authed, monkeypatch):
    f = Spoolman()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    p = authed.post("/api/printers", json={"name": "Spool", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    linked = authed.post("/api/filament", json={"material": "PLA", "external_id": "spoolman:12"}).json()
    unlinked = authed.post("/api/filament", json={"material": "PLA"}).json()
    try:
        url = f"/api/printers/{p['id']}/active-spool"
        r = authed.post(url, json={"filament_id": linked["id"]})
        assert r.status_code == 200 and r.json() == {"status": "sent", "spoolman_id": 12} and f.told == [{"spool_id": 12}]
        assert "Spoolman" in authed.post(url, json={"filament_id": unlinked["id"]}).json()["detail"] and authed.post(url, json={"filament_id": 999999}).status_code == 400
        assert authed.post(url, json={}).status_code == 400 and authed.post(url, json={"filament_id": True}).status_code == 400
        assert authed.post("/api/printers/999999/active-spool", json={"filament_id": linked["id"]}).status_code == 404
        octo = authed.post("/api/printers", json={"name": "Octo", "kind": "octoprint", "url": "http://octopi.local", "api_key": "k12345678"}).json()
        assert "Only Klipper" in authed.post(f"/api/printers/{octo['id']}/active-spool", json={"filament_id": linked["id"]}).json()["detail"]
        assert any("active spool set" in a["summary"] for a in authed.get("/api/activity").json()["items"])
    finally:
        for f_ in (linked, unlinked):
            authed.delete(f"/api/filament/{f_['id']}")
        for pr in authed.get("/api/printers").json()["printers"]:
            authed.delete(f"/api/printers/{pr['id']}")


# ---------------------------------------------------------------- the dispatch watchdog
def _sent(authed, farm_fixture, minutes_ago=2, state="standby", online=True):
    fake, a, b, m, _ = farm_fixture
    item = _queue(authed, farm_fixture, printer_id=a["id"])
    authed.patch(f"/api/queue/{item['id']}", json={"status": "printing"})
    with Session(engine) as s:
        job = PrinterJob(printer_id=a["id"], filename="x.gcode", model_id=m["id"], started=True, sent_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
        s.add(job)
        s.commit()
        s.refresh(job)
    printwatch.latest[a["id"]] = {"online": online, "state": state, "name": "Alpha"}
    return item, job.id


def test_a_print_that_never_began_goes_back_in_the_queue(authed, farm, monkeypatch):
    told = []
    monkeypatch.setattr(dispatch_watch, "notify_event", lambda session, event, title, message, **kw: told.append((event, title, message)))
    item, job_id = _sent(authed, farm)
    with Session(engine) as s:
        assert len(dispatch_watch.scheduled(s)) == 1
        job = s.get(PrinterJob, job_id)
        assert job.outcome == "not_started" and job.finished_at is not None
    back = next(q for q in authed.get("/api/queue").json() if q["id"] == item["id"])
    assert back["status"] == "queued" and "never started" in back["start_note"]
    assert told and told[0][0] == "dispatch_failed" and "back in the queue" in told[0][2]
    assert any("never started" in a["summary"] for a in authed.get("/api/activity").json()["items"])
    with Session(engine) as s:
        assert dispatch_watch.scheduled(s) == []                                                                                      # once only


def test_the_watchdog_leaves_alone_what_is_fine_or_cannot_be_judged(authed, farm):
    with Session(engine) as s:
        item, _ = _sent(authed, farm, minutes_ago=0)
        assert dispatch_watch.scheduled(s) == []                                                                                      # too soon
    item2, job2 = _sent(authed, farm, state="printing")
    with Session(engine) as s:
        assert dispatch_watch.scheduled(s) == []                                                                                      # it is printing
    printwatch.latest[farm[1]["id"]]["state"] = "standby"
    with Session(engine) as s:
        assert dispatch_watch.scheduled(s) == []                                                                                      # it was seen printing, so it did begin (and ended)
    dispatch_watch.reset()
    printwatch.latest[farm[1]["id"]]["online"] = False
    with Session(engine) as s:
        assert dispatch_watch.scheduled(s) == []                                                                                      # a printer that cannot be asked proves nothing
    printwatch.latest[farm[1]["id"]]["online"] = True
    with Session(engine) as s:
        old = s.get(PrinterJob, job2)
        old.sent_at = datetime.utcnow() - timedelta(minutes=30)
        s.add(old)
        s.commit()
        assert dispatch_watch.scheduled(s) == []                                                                                      # too old to say anything about
        old.sent_at = datetime.utcnow() - timedelta(minutes=3)
        s.add(old)
        s.commit()
    authed.put("/api/settings", json={"dispatch_watchdog": "false"})
    with Session(engine) as s:
        assert dispatch_watch.scheduled(s) == []                                                                                      # switched off
    assert next(q for q in authed.get("/api/queue").json() if q["id"] == item2["id"])["status"] == "printing"


def test_a_job_without_a_queue_entry_is_still_marked_and_reported(authed, farm, monkeypatch):
    fake, a, b, m, _ = farm
    told = []
    monkeypatch.setattr(dispatch_watch, "notify_event", lambda session, event, title, message, **kw: told.append(message))
    with Session(engine) as s:
        job = PrinterJob(printer_id=a["id"], filename="orphan.gcode", model_id=None, started=True, sent_at=datetime.utcnow() - timedelta(minutes=2))
        s.add(job)
        s.commit()
        printwatch.latest[a["id"]] = {"online": True, "state": "ready"}
        assert dispatch_watch.scheduled(s) == ["orphan.gcode"]
    assert told and "Check the printer" in told[0]


def test_the_scheduler_runs_it_and_the_controls_are_in_the_page():
    from pathlib import Path
    from app import scheduler
    assert "dispatch" in scheduler.INTERVALS and "dispatch" in scheduler._jobs()
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html, js = (root / "index.html").read_text(encoding="utf-8"), (root / "app.js").read_text(encoding="utf-8")
    for element in ("group-own-only-help", "f-known"):
        assert f'id="{element}"' in html, element
    for needle in ("own_only", "needs_review", "/review", "requires", "capabilities", "known_status", "recommended", "/api/nfc/lookup", "/api/nfc/claim", "/api/nfc/create", "active-spool", "nfc-scan", "queue-approve"):
        assert needle in js, needle
