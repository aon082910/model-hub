"""Staggered starts and a limit on printers printing together, pausing a suspected failure, and the page's NFC controls."""
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import Session

from app import failure_watch, printers as printing, printwatch, stagger
from app.db import engine
from app.models import Printer, PrinterJob
from test_control_fit_offsite import Controllable
from test_print_farm import farm, _keep, _queue  # noqa: F401  (the fixture and helpers)

PW = "a long enough password"


@pytest.fixture()
def fake(monkeypatch, authed):
    f = Controllable()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    printwatch.reset()
    failure_watch.reset()
    yield f
    printwatch.reset()
    failure_watch.reset()
    authed.put("/api/settings", json={"stagger_minutes": "", "max_printing": ""})
    for q in authed.get("/api/queue").json():
        authed.delete(f"/api/queue/{q['id']}")
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")
    with Session(engine) as s:
        for job in s.query(PrinterJob).all():
            s.delete(job)
        s.commit()


def _printer(c, name="P1", url="http://klipper.local:7125", **extra):
    r = c.post("/api/printers", json={"name": name, "kind": "moonraker", "url": url, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _job(printer_id, minutes_ago, started=True, finished=False):
    with Session(engine) as s:
        s.add(PrinterJob(printer_id=printer_id, filename="a.gcode", started=started, sent_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
                         finished_at=datetime.utcnow() if finished else None))
        s.commit()


# ---------------------------------------------------------------- staggered starts
def test_nothing_waits_until_a_gap_or_limit_is_set(authed, fake):
    a, b = _printer(authed, "A"), _printer(authed, "B", "http://octopi.local")
    _job(a["id"], 1)
    printwatch.latest[a["id"]] = {"state": "printing"}
    with Session(engine) as s:
        assert stagger.reason_to_wait(s, s.get(Printer, b["id"])) is None


def test_a_recent_start_on_another_printer_makes_this_one_wait(authed, fake):
    authed.put("/api/settings", json={"stagger_minutes": "10"})
    a, b = _printer(authed, "Alpha"), _printer(authed, "Beta", "http://octopi.local")
    _job(a["id"], 3)
    with Session(engine) as s:
        why = stagger.reason_to_wait(s, s.get(Printer, b["id"]))
        assert "Alpha" in why and "wait 7 more minutes" in why and "10 minutes apart" in why
        assert stagger.reason_to_wait(s, s.get(Printer, a["id"])) is None                    # its own start does not count
        assert stagger.reason_to_wait(s, s.get(Printer, b["id"]), now=datetime.utcnow() + timedelta(minutes=8)) is None
    with Session(engine) as s:
        job = s.query(PrinterJob).first()
        job.finished_at = datetime.utcnow()
        s.add(job)
        s.commit()
        assert stagger.reason_to_wait(s, s.get(Printer, b["id"])) is None                    # a print that ended no longer counts


def test_unstarted_files_and_old_starts_do_not_count(authed, fake):
    authed.put("/api/settings", json={"stagger_minutes": "10"})
    a, b = _printer(authed, "A"), _printer(authed, "B", "http://octopi.local")
    _job(a["id"], 2, started=False)
    _job(a["id"], 30)
    with Session(engine) as s:
        assert stagger.reason_to_wait(s, s.get(Printer, b["id"])) is None


def test_a_limit_on_printers_printing_together(authed, fake):
    authed.put("/api/settings", json={"max_printing": "1"})
    a, b = _printer(authed, "A"), _printer(authed, "B", "http://octopi.local")
    printwatch.latest[a["id"]] = {"state": "printing"}
    with Session(engine) as s:
        assert "1 printer is already printing and at most 1 may print at once" == stagger.reason_to_wait(s, s.get(Printer, b["id"]))
    printwatch.latest[a["id"]] = {"state": "idle"}
    with Session(engine) as s:
        assert stagger.reason_to_wait(s, s.get(Printer, b["id"])) is None


def test_junk_settings_mean_off(authed, fake):
    authed.put("/api/settings", json={"stagger_minutes": "soon", "max_printing": "-3"})
    a, b = _printer(authed, "A"), _printer(authed, "B", "http://octopi.local")
    _job(a["id"], 1)
    with Session(engine) as s:
        assert stagger.reason_to_wait(s, s.get(Printer, b["id"])) is None


def test_the_queue_refuses_a_start_that_is_too_soon_unless_forced(authed, farm):
    fake, a, b, m, _ = farm
    authed.put("/api/settings", json={"stagger_minutes": "10"})
    try:
        _keep(authed, m)
        item = _queue(authed, farm, printer_id=b["id"])
        _job(a["id"], 2)
        fake.moonraker_state = "standby"
        r = authed.post(f"/api/queue/{item['id']}/send", json={"start": True})
        assert r.status_code == 409 and r.json()["detail"].startswith("Wait: ") and "Alpha" in r.json()["detail"]
        assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200            # sending without starting is fine
        forced = authed.post(f"/api/queue/{item['id']}/send", json={"start": True, "force": True})
        assert forced.status_code in (200, 409) and "Wait:" not in forced.text
    finally:
        authed.put("/api/settings", json={"stagger_minutes": ""})
        with Session(engine) as s:
            for job in s.query(PrinterJob).all():
                s.delete(job)
            s.commit()


def test_the_settings_are_in_the_page():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")
    for element in ("stagger-minutes", "max-printing", "stagger-save"):
        assert f'id="{element}"' in html, element
    assert "stagger_minutes" in js and "max_printing" in js and "force: true" in js and "Start it anyway" in js


# ---------------------------------------------------------------- pausing a suspected failure
def _watched(c, **extra):
    return _printer(c, "Cam", snapshot_url="http://cam.local/snap.jpg", **extra)


def test_pausing_needs_watching_and_is_off_by_default(authed, fake):
    p = _printer(authed, "Cam", snapshot_url="http://cam.local/snap.jpg")
    assert p["pause_on_failure"] is False
    assert authed.patch(f"/api/printers/{p['id']}", json={"pause_on_failure": True}).status_code == 400
    assert authed.patch(f"/api/printers/{p['id']}", json={"watch_failures": True, "pause_on_failure": "yes"}).status_code == 400
    both = authed.patch(f"/api/printers/{p['id']}", json={"watch_failures": True, "pause_on_failure": True}).json()
    assert both["watch_failures"] is True and both["pause_on_failure"] is True
    off = authed.patch(f"/api/printers/{p['id']}", json={"watch_failures": False}).json()
    assert off["pause_on_failure"] is False                                                                 # watching stopped: so does pausing


def _run_looks(c, fake, monkeypatch, pause, answers):
    p = _watched(c)
    c.patch(f"/api/printers/{p['id']}", json={"watch_failures": True, "pause_on_failure": pause})
    told, calls = [], []
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: b"jpeg")
    monkeypatch.setattr(failure_watch, "notify_event", lambda session, event, title, message: told.append((title, message)))
    monkeypatch.setattr(printing, "control", lambda kind, url, key, serial, action: calls.append(action))
    seq = iter(answers)
    monkeypatch.setattr(failure_watch, "verdict", lambda session, picture: next(seq))
    printwatch.latest[p["id"]] = {"state": "printing", "file": "x.gcode", "progress": 20}
    for _ in answers:
        with Session(engine) as s:
            failure_watch.scheduled(s)
    return told, calls


def test_a_pause_comes_after_three_bad_looks_not_two(authed, fake, monkeypatch):
    bad = {"failed": True, "reason": "spaghetti"}
    told, calls = _run_looks(authed, fake, monkeypatch, True, [bad, bad])
    assert calls == [] and len(told) == 1 and "may have a failed print" in told[0][0]                       # two looks: only a warning


def test_the_third_look_pauses_once_and_says_so(authed, fake, monkeypatch):
    bad = {"failed": True, "reason": "spaghetti"}
    told, calls = _run_looks(authed, fake, monkeypatch, True, [bad, bad, bad, bad])
    assert calls == ["pause"] and len(told) == 2 and "paused" in told[1][0] and "has been paused" in told[1][1]


def test_without_the_switch_it_never_pauses(authed, fake, monkeypatch):
    bad = {"failed": True, "reason": "x"}
    told, calls = _run_looks(authed, fake, monkeypatch, False, [bad, bad, bad, bad])
    assert calls == [] and len(told) == 1


def test_a_pause_that_fails_is_reported_not_hidden(authed, fake, monkeypatch):
    bad = {"failed": True, "reason": "x"}

    def refuse(kind, url, key, serial, action):
        raise printing.PrinterError("The printer refused (409)")
    p = _watched(authed)
    authed.patch(f"/api/printers/{p['id']}", json={"watch_failures": True, "pause_on_failure": True})
    told = []
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: b"jpeg")
    monkeypatch.setattr(failure_watch, "notify_event", lambda session, event, title, message: told.append((title, message)))
    monkeypatch.setattr(printing, "control", refuse)
    monkeypatch.setattr(failure_watch, "verdict", lambda session, picture: bad)
    printwatch.latest[p["id"]] = {"state": "printing", "file": "x", "progress": 5}
    for _ in range(4):
        with Session(engine) as s:
            failure_watch.scheduled(s)
    assert len(told) == 2 and "could not be paused" in told[1][1] and "refused" in told[1][1]


def test_a_good_look_in_between_starts_the_count_again(authed, fake, monkeypatch):
    bad, good = {"failed": True, "reason": "x"}, {"failed": False, "reason": ""}
    told, calls = _run_looks(authed, fake, monkeypatch, True, [bad, bad, good, bad, bad])
    assert calls == [] and len(told) == 1


# ---------------------------------------------------------------- NFC tags
def test_the_nfc_controls_are_in_the_page_and_only_for_phones_that_have_nfc():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "NFC_OK" in js and "NDEFReader" in js and "nfcWriteSpool" in js and "nfcScanSpool" in js and "fil-nfc" in js and "nfc-scan" in js
    assert "printer-pause-watch" in js and "pause_on_failure" in js
    assert r"/^#\/spool\/\d+$/" in js and "url.origin === location.origin" in js           # only this server's own spool links are followed
