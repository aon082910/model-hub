"""Jobs that run by themselves: scheduled backups, notifications, designer checks, low stock, printer completion."""
import json
import time

import httpx
import pytest

from app import auto_backup, backup, downloads, follow_watch, printwatch, scheduler, sources, stock_watch
from app import notify as notify_module
from test_printers import FakePrinter
from test_sources import FakeSites, _user_print


@pytest.fixture(autouse=True)
def clean(authed, monkeypatch):
    sent = []
    monkeypatch.setattr(notify_module.httpx, "post", lambda url, **kw: sent.append((url, kw["json"]["title"], kw["json"]["message"])))
    authed.put("/api/settings", json={"notify_webhook_url": "http://hook.example/x"})
    scheduler.reset()
    printwatch.reset()
    yield sent
    authed.put("/api/settings", json={
        "notify_webhook_url": "", "auto_backup": "", "auto_backup_last": "", "auto_backup_keep": "", "low_filament_g": "",
        "low_stock_notified": "", "auto_listing_check": "", **{f"notify_{e}": "" for e in notify_module.EVENTS}})
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")


def _session():
    from sqlmodel import Session
    from app.db import engine
    return Session(engine)


# ---------- the scheduler itself ----------

def test_jobs_run_when_due_and_not_before(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "_jobs", lambda: {"printers": lambda s: calls.append("p"), "backup": lambda s: calls.append("b")})
    assert scheduler.run_due(now=100_000) == ["printers", "backup"]
    assert scheduler.run_due(now=100_010) == []                                   # too soon
    assert scheduler.run_due(now=100_000 + scheduler.INTERVALS["printers"]) == ["printers"]
    assert scheduler.run_due(now=100_000 + scheduler.INTERVALS["backup"] + 1, only=["backup"]) == ["backup"]
    assert calls == ["p", "b", "p", "b"]


def test_a_failing_job_does_not_stop_the_others(monkeypatch):
    calls = []

    def boom(session):
        raise RuntimeError("broken job")
    monkeypatch.setattr(scheduler, "_jobs", lambda: {"printers": boom, "backup": lambda s: calls.append("b")})
    assert scheduler.run_due(now=500_000) == ["backup"]
    assert calls == ["b"]


def test_every_named_job_exists():
    assert set(scheduler._jobs()) == set(scheduler.INTERVALS)


# ---------- notification switches ----------

def test_events_can_be_switched_off(authed, clean):
    with _session() as s:
        assert notify_module.notify_event(s, "low_stock", "t", "m") is True
        authed.put("/api/settings", json={"notify_low_stock": "false"})
        assert notify_module.notify_event(s, "low_stock", "t", "m") is False
        assert notify_module.notify_event(s, "no_such_event", "t", "m") is False
    assert len(clean) == 1


def test_the_settings_page_lists_events_and_can_send_a_test(authed, clean):
    events = authed.get("/api/settings/notify-events").json()["events"]
    assert {e["id"] for e in events} == set(notify_module.EVENTS) and all(e["enabled"] for e in events)
    authed.put("/api/settings", json={"notify_print_done": "false"})
    assert next(e for e in authed.get("/api/settings/notify-events").json()["events"] if e["id"] == "print_done")["enabled"] is False
    assert authed.post("/api/settings/notify-test").json() == {"status": "sent"}
    assert clean[-1][1] == "Model Hub: test"
    authed.put("/api/settings", json={"notify_webhook_url": ""})
    assert authed.post("/api/settings/notify-test").status_code == 400


# ---------- scheduled backups ----------

def test_a_weekly_backup_is_made_once_then_waits(authed):
    with _session() as s:
        assert auto_backup.maybe_backup(s, now=10_000_000) is True            # nothing made yet: due at once (default is weekly)
        assert auto_backup.maybe_backup(s, now=10_000_000 + 3600) is False
        assert auto_backup.maybe_backup(s, now=10_000_000 + 6 * 24 * 3600) is False
        assert auto_backup.maybe_backup(s, now=10_000_000 + 7 * 24 * 3600 + 1) is True
    names = [b["name"] for b in authed.get("/api/backup/saved").json()["backups"] if b["name"].startswith("auto-backup-")]
    assert len(names) == 2


def test_backups_can_be_off_or_daily_and_only_the_newest_are_kept(authed):
    with _session() as s:
        authed.put("/api/settings", json={"auto_backup": "off"})
        assert auto_backup.maybe_backup(s, now=20_000_000) is False
        authed.put("/api/settings", json={"auto_backup": "daily", "auto_backup_keep": "2"})
        for day in range(4):
            assert auto_backup.maybe_backup(s, now=20_000_000 + day * 86400 + 5) is True
    kept = [b["name"] for b in authed.get("/api/backup/saved").json()["backups"] if b["name"].startswith("auto-backup-")]
    assert len(kept) == 2


def test_scheduled_pruning_leaves_other_kinds_of_copy_alone(authed):
    authed.post("/api/backup/save")
    authed.put("/api/settings", json={"auto_backup": "daily", "auto_backup_keep": "1"})
    with _session() as s:
        auto_backup.maybe_backup(s, now=30_000_000)
        auto_backup.maybe_backup(s, now=30_000_000 + 90000)
    names = [b["name"] for b in authed.get("/api/backup/saved").json()["backups"]]
    assert sum(n.startswith("auto-backup-") for n in names) == 1 and sum(n.startswith("modelhub-backup-") for n in names) == 1


def test_a_failed_backup_is_retried_later_and_reported(authed, clean, monkeypatch):
    def broken(prefix="x"):
        raise OSError("disk full")
    monkeypatch.setattr(backup, "save_backup", broken)
    with _session() as s:
        assert auto_backup.maybe_backup(s, now=40_000_000) is False
        assert auto_backup.maybe_backup(s, now=40_000_000 + 3600) is False           # not hammered every hour
    assert [m for m in clean if "backup failed" in m[1]] and len([m for m in clean if "backup failed" in m[1]]) == 1


# ---------- followed designers ----------

def test_new_uploads_from_followed_designers_are_announced(authed, clean, monkeypatch):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    d = authed.post("/api/designers", json={"provider": "printables", "handle": "16", "name": "Prusa Research"}).json()
    try:
        with _session() as s:
            assert follow_watch.check_and_notify(s) == 0 and clean == []
        fake.printables_user_prints = [_user_print(900, "Brand new thing")] + fake.printables_user_prints
        with _session() as s:
            assert follow_watch.check_and_notify(s) == 1
        assert clean[-1][1] == "Model Hub: new uploads" and "Prusa Research (1)" in clean[-1][2]
        with _session() as s:
            assert follow_watch.check_and_notify(s) == 0
    finally:
        authed.delete(f"/api/designers/{d['id']}")


# ---------- low stock ----------

def test_low_spools_and_supplies_are_announced_once(authed, clean):
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "stockwatch", "spool_weight_g": 1000, "remaining_g": 40}).json()
    item = authed.post("/api/inventory", json={"name": "stockwatch screws", "quantity": 2, "min_quantity": 5}).json()
    try:
        with _session() as s:
            fresh = stock_watch.check_and_notify(s)
        assert any("stockwatch" in t and "40 g left" in t for t in fresh) and any("stockwatch screws" in t for t in fresh)
        count = len(clean)
        with _session() as s:
            assert stock_watch.check_and_notify(s) == [] and len(clean) == count          # not nagging
        authed.patch(f"/api/filament/{spool['id']}", json={"remaining_g": 900})
        with _session() as s:
            stock_watch.check_and_notify(s)                                               # restocked: forgotten
        authed.patch(f"/api/filament/{spool['id']}", json={"remaining_g": 10})
        with _session() as s:
            assert any("10 g left" in t for t in stock_watch.check_and_notify(s))        # low again: announced again
    finally:
        authed.delete(f"/api/filament/{spool['id']}")
        authed.delete(f"/api/inventory/{item['id']}")


def test_a_zero_threshold_turns_spool_warnings_off(authed, clean):
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "stockwatch2", "spool_weight_g": 1000, "remaining_g": 5}).json()
    authed.put("/api/settings", json={"low_filament_g": "0"})
    try:
        with _session() as s:
            assert not [t for t in stock_watch.check_and_notify(s) if "stockwatch2" in t]
    finally:
        authed.delete(f"/api/filament/{spool['id']}")


# ---------- automatic listing checks ----------

def test_listing_checks_are_off_unless_asked_for(authed, monkeypatch):
    from app import source_updates, source_updates_auto
    started = []
    monkeypatch.setattr(source_updates, "start_job", lambda force=False, notify_changes=False: started.append(notify_changes))
    with _session() as s:
        assert source_updates_auto.maybe_start(s) is False
        authed.put("/api/settings", json={"auto_listing_check": "true"})
        assert source_updates_auto.maybe_start(s) is True
    assert started == [True]


# ---------- printers finishing prints ----------

@pytest.fixture()
def printer(authed, monkeypatch):
    from app import printers as printing
    fake = FakePrinter()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(fake), timeout=timeout))
    p = authed.post("/api/printers", json={"name": "Watchy", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    yield fake, p
    authed.delete(f"/api/printers/{p['id']}")


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


def _poll():
    with _session() as s:
        return printwatch.poll(s)


def _send_model_job(authed, p, model_id, filename):
    from app.models import PrinterJob
    with _session() as s:
        s.add(PrinterJob(printer_id=p["id"], filename=filename, model_id=model_id, started=True))
        s.commit()


def test_a_finished_print_is_logged_and_announced(authed, clean, printer):
    fake, p = printer
    m = authed.post("/api/library/import", files={"file": ("watched.stl", _stl(97), "application/octet-stream")}).json()
    try:
        _send_model_job(authed, p, m["id"], "benchy.gcode")
        fake.moonraker_state = "printing"
        assert _poll() == []                                          # the first look only learns the state
        assert _poll() == []
        fake.moonraker_state = "complete"
        assert _poll() == [("Watchy", "done")]
        logs = authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"]
        assert len(logs) == 1 and logs[0]["source"] == "printer" and logs[0]["minutes"] == pytest.approx(754 / 60)
        assert "finished" in clean[-1][1] and m["filename"] in clean[-1][2]
        assert _poll() == []                                          # not reported twice
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_finished_print_completes_the_waiting_queue_entry(authed, clean, printer):
    fake, p = printer
    spool = authed.post("/api/filament", json={"material": "PLA", "color": "pw", "spool_weight_g": 1000, "remaining_g": 500}).json()
    m = authed.post("/api/library/import", files={"file": ("queued part.stl", _stl(98), "application/octet-stream")}).json()
    item = authed.post("/api/queue", json={"model_id": m["id"], "filament_id": spool["id"], "estimated_grams": 30}).json()
    try:
        _send_model_job(authed, p, m["id"], "benchy.gcode")
        _poll()
        fake.moonraker_state = "complete"
        _poll()
        assert next(i for i in authed.get("/api/queue").json() if i["id"] == item["id"])["status"] == "done"
        assert next(f for f in authed.get("/api/filament").json() if f["id"] == spool["id"])["remaining_g"] == 470
        logs = authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"]
        assert len(logs) == 1 and logs[0]["source"] == "queue"        # one entry, written by the queue
    finally:
        authed.delete(f"/api/queue/{item['id']}")
        authed.delete(f"/api/library/models/{m['id']}")
        authed.delete(f"/api/filament/{spool['id']}")


def test_a_cancelled_print_is_announced_and_logged_as_a_failure(authed, clean, printer):
    fake, p = printer
    m = authed.post("/api/library/import", files={"file": ("cancelled.stl", _stl(99), "application/octet-stream")}).json()
    try:
        _send_model_job(authed, p, m["id"], "benchy.gcode")
        _poll()
        fake.moonraker_state = "cancelled"
        assert _poll() == [("Watchy", "stopped")]
        logs = authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"]
        assert len(logs) == 1 and logs[0]["outcome"] == "failed" and logs[0]["source"] == "printer" and logs[0]["failure_reason"] is None
        assert authed.get(f"/api/library/models/{m['id']}").json()["print_count"] == 0             # a failure is not a print
        assert "stopped" in clean[-1][1]
    finally:
        authed.delete(f"/api/library/models/{m['id']}")


def test_a_file_that_was_not_sent_from_a_model_is_only_announced(authed, clean, printer):
    fake, p = printer
    _poll()
    fake.moonraker_state = "complete"
    assert _poll() == [("Watchy", "done")]
    assert clean[-1][1].endswith("finished") and "benchy.gcode" in clean[-1][2]


def test_an_unreachable_printer_does_not_forget_what_it_was_doing(authed, clean, printer):
    fake, p = printer
    _poll()
    fake.fail = httpx.ConnectError("down")
    assert _poll() == []
    fake.fail = None
    fake.moonraker_state = "complete"
    assert _poll() == [("Watchy", "done")]


def test_octoprint_finishing_is_judged_by_progress(authed, clean, monkeypatch):
    from app import printers as printing
    assert printwatch.finished("octoprint", "ready", 100.0) is True
    assert printwatch.finished("octoprint", "ready", 40.0) is False
    assert printwatch.finished("octoprint", "ready", None) is False
    assert printwatch.finished("moonraker", "complete", None) is True and printwatch.finished("moonraker", "standby", 100) is False


def test_sending_a_model_remembers_the_job(authed, printer, tmp_path, monkeypatch):
    import stat
    fake, p = printer
    script = tmp_path / "slicer.sh"
    script.write_text('#!/bin/sh\nout=""; while [ $# -gt 0 ]; do if [ "$1" = "-o" ]; then out="$2"; fi; shift; done; echo G28 > "$out"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "p.ini").write_text("x")
    monkeypatch.setenv("SLICER_CLI_PATH", str(script))
    monkeypatch.setenv("SLICER_CONFIG_PATH", str(tmp_path / "p.ini"))
    m = authed.post("/api/library/import", files={"file": ("remembered.stl", _stl(101), "application/octet-stream")}).json()
    try:
        r = authed.post(f"/api/printers/{p['id']}/send", data={"model_id": str(m["id"]), "start": "true"})
        assert r.status_code == 200, r.text
        from sqlmodel import select
        from app.models import PrinterJob
        with _session() as s:
            job = s.exec(select(PrinterJob).where(PrinterJob.model_id == m["id"])).first()
            assert job and job.filename == r.json()["filename"] and job.started is True
    finally:
        authed.delete(f"/api/library/models/{m['id']}")
