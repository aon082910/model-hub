"""Quiet hours and the daily digest, e-mail, printers out of service, the camera wall, temperature history, finding printers, diagnosing one, the log and the support bundle."""
import io
import json
import re
import socket
import uuid
import zipfile
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, select
from starlette.testclient import TestClient

from app import camera_wall, channels, digest, discovery, logbuffer, notify, printers as printing, printwatch, stagger, temps
from app.db import engine
from app.models import HeldNotice, Printer, TempSample
from test_print_farm import farm, _keep, _queue  # noqa: F401  (the fixture and helpers)

PW = "a long enough password"
SETTING_KEYS = ("quiet_start", "quiet_end", "digest_daily", "digest_time", "digest_last", "notify_webhook_url", "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password",
                "smtp_from", "smtp_to", "wall_token", "notify_print_started")


def _app():
    from app.main import app
    return app


@pytest.fixture()
def tidy(authed):
    yield
    authed.put("/api/settings", json={k: "" for k in SETTING_KEYS})
    with Session(engine) as s:
        for r in s.exec(select(HeldNotice)).all():
            s.delete(r)
        for r in s.exec(select(TempSample)).all():
            s.delete(r)
        s.commit()
    printwatch.reset()
    camera_wall._cache.clear()
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")


class Hook:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return httpx.Response(200, json={})


@pytest.fixture()
def hook(monkeypatch):
    h = Hook()
    monkeypatch.setattr(httpx, "post", h.post)
    return h


def _at(h, m=0):
    return datetime(2026, 10, 10, h, m)


# ---------------------------------------------------------------- quiet hours
def test_quiet_hours_wrap_midnight_and_ignore_nonsense(authed, tidy):
    def window(start, end):
        authed.put("/api/settings", json={"quiet_start": start, "quiet_end": end})
        with Session(engine) as s:
            return lambda h, m=0: digest.in_quiet(s, _at(h, m)), s
    check, s = window("22:00", "07:00")
    assert [check(h) for h in (21, 22, 23, 0, 6, 7, 12)] == [False, True, True, True, True, False, False] and check(6, 59) is True and check(21, 59) is False
    authed.put("/api/settings", json={"quiet_start": "13:00", "quiet_end": "14:30"})
    with Session(engine) as s:
        assert [digest.in_quiet(s, _at(h, m)) for h, m in ((12, 59), (13, 0), (14, 29), (14, 30))] == [False, True, True, False]
    for bad in (("", ""), ("22:00", ""), ("25:00", "07:00"), ("10:00", "10:00"), ("ten", "eleven"), ("7:5", "8:00")):
        authed.put("/api/settings", json={"quiet_start": bad[0], "quiet_end": bad[1]})
        with Session(engine) as s:
            assert digest.in_quiet(s, _at(10, 0)) is False and digest.quiet_hours(s) is None, bad
    assert digest.valid_time("07:30") and digest.valid_time("7:30") and not digest.valid_time("24:00") and not digest.valid_time("7:3")


def test_an_alarm_is_never_held_and_ordinary_messages_wait_for_the_end_of_quiet_hours(authed, tidy, hook):
    authed.put("/api/settings", json={"notify_webhook_url": "https://ntfy.sh/t", "quiet_start": "00:00", "quiet_end": "23:59"})         # (quiet nearly all day: the real clock is used by notify)
    now = datetime.now()
    quiet_now = now.hour * 60 + now.minute < 23 * 60 + 59
    with Session(engine) as s:
        assert digest.should_hold(s, "alarm", now) is False
        assert digest.should_hold(s, "normal", now) is quiet_now and digest.should_hold(s, "silent", now) is quiet_now
        if not quiet_now:
            pytest.skip("it is the one quiet minute-boundary of the day")
        assert notify.notify(s, "Model Hub: print done", "benchy is done", level="normal", event="print_done") is True
        assert notify.notify(s, "Model Hub: started", "x started", level="silent", event="print_started") is True
        assert hook.calls == [] and len(digest.held(s)) == 2                                                       # kept, nothing sent
        assert notify.notify(s, "Model Hub: backup failed", "oops", level="alarm", event="backup_failed") is True
        assert len(hook.calls) == 1 and hook.calls[0][1]["json"]["level"] == "alarm"
        assert notify.notify(s, "Model Hub: test", "now", hold=False) is True and len(hook.calls) == 2              # a test goes at once
        assert digest.release_due(s, _at(12)) == 0 and len(hook.calls) == 2                                          # still quiet
        authed.put("/api/settings", json={"quiet_start": "01:00", "quiet_end": "02:00"})
        assert digest.release_due(s, _at(12)) == 2
        assert len(hook.calls) == 3
        body = hook.calls[2][1]["json"]
        assert body["title"] == "Model Hub: 2 messages while you were away" and "print done: benchy is done" in body["message"] and "started: x started" in body["message"]
        assert digest.held(s) == [] and digest.release_due(s, _at(12)) == 0


def test_nothing_is_held_when_nothing_could_be_sent(authed, tidy, hook):
    authed.put("/api/settings", json={"quiet_start": "00:00", "quiet_end": "23:59"})                        # no webhook and no channel is set up
    with Session(engine) as s:
        assert notify.notify(s, "t", "m") is False and digest.held(s) == [] and hook.calls == []


def test_the_daily_digest_collects_the_day_and_sends_once(authed, tidy, hook):
    authed.put("/api/settings", json={"notify_webhook_url": "https://ntfy.sh/t", "digest_daily": "true", "digest_time": "08:00"})
    with Session(engine) as s:
        assert digest.daily_time(s) == 480
        notify.notify(s, "Model Hub: one", "first", event="print_done")
        notify.notify(s, "Model Hub: two", "second", event="print_done")
        assert hook.calls == [] and len(digest.held(s)) == 2
        for row in digest.held(s):                                                                             # held yesterday evening
            row.at = datetime(2026, 10, 9, 20, 0)
            s.add(row)
        s.commit()
        assert digest.release_due(s, _at(7, 59)) == 0                                                          # not yet
        assert digest.release_due(s, _at(8, 0)) == 2 and len(hook.calls) == 1
        assert digest.release_due(s, _at(8, 5)) == 0
        notify.notify(s, "Model Hub: three", "third", event="print_done")                                      # held after today's digest
        for row in digest.held(s):
            row.at = _at(10, 0)
            s.add(row)
        s.commit()
        assert digest.release_due(s, _at(10, 30)) == 0 and len(digest.held(s)) == 1                            # waits for tomorrow
        assert digest.release_due(s, _at(8, 0) + timedelta(days=1)) == 1
        assert notify.notify(s, "Model Hub: alarm", "now", level="alarm") is True and len(hook.calls) == 3       # alarms are never kept back
    authed.put("/api/settings", json={"digest_time": "banana"})
    with Session(engine) as s:
        assert digest.daily_time(s) == 480                                                                     # a bad time means 08:00


def test_a_digest_is_capped_and_the_held_list_is_bounded(authed, tidy):
    with Session(engine) as s:
        for n in range(digest.MAX_HELD + 30):
            digest.hold(s, f"T{n}", f"M{n}", "normal", "x")
        rows = digest.held(s)
        assert len(rows) == digest.MAX_HELD and rows[0].title == "T30"                                          # the oldest are dropped
        title, text = digest.compose(rows)
        assert title.startswith("Model Hub: 200 messages") and text.splitlines()[-1] == "... and 175 more" and len(text.splitlines()) == digest.LINES_IN_DIGEST + 1


def test_the_scheduler_runs_the_digest_and_the_page_has_its_fields():
    from app import scheduler
    assert "digest" in scheduler.INTERVALS and "digest" in scheduler._jobs()


# ---------------------------------------------------------------- e-mail
class Smtp:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.ssl, self.log, self.sent = host, port, context is not None, [], []
        Smtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.log.append("starttls")

    def login(self, user, password):
        self.log.append(("login", user, password))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture()
def smtp(monkeypatch):
    import smtplib
    Smtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=None: Smtp(host, port, timeout))
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda host, port, timeout=None, context=None: Smtp(host, port, timeout, context or True))
    return Smtp


def _mail(authed, **extra):
    authed.put("/api/settings", json={"smtp_host": "smtp.example.com", "smtp_from": "hub@example.com", "smtp_to": "me@example.com, you@example.org", "smtp_user": "hubuser", "smtp_password": "pw-secret-1", **extra})


def test_e_mail_is_a_channel_only_when_fully_set_up(authed, tidy):
    with Session(engine) as s:
        assert "email" not in channels.configured(s)
    authed.put("/api/settings", json={"smtp_host": "smtp.example.com", "smtp_from": "hub@example.com"})
    with Session(engine) as s:
        assert "email" not in channels.configured(s)                                                            # no recipient
    for bad in ("not an address", "a@b", "x@y.z", "<me>@example.com", 'me@exam"ple.com'):
        authed.put("/api/settings", json={"smtp_to": bad})
        with Session(engine) as s:
            assert channels.email_addresses(s) == [] and "email" not in channels.configured(s), bad
    authed.put("/api/settings", json={"smtp_to": "me@example.com; you@example.org,  me@example.com", "smtp_host": "bad host"})
    with Session(engine) as s:
        assert "email" not in channels.configured(s)
    authed.put("/api/settings", json={"smtp_host": "smtp.example.com"})
    with Session(engine) as s:
        assert "email" in channels.configured(s) and channels.email_addresses(s) == ["me@example.com", "you@example.org", "me@example.com"]


def test_a_message_goes_by_starttls_with_the_login_and_the_loudness(authed, tidy, smtp):
    _mail(authed)
    with Session(engine) as s:
        assert channels._email(s, "Model Hub: Voron\r\nBcc: evil@example.com", "It is done", None, "alarm") is True
    server = Smtp.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 587) and server.log == ["starttls", ("login", "hubuser", "pw-secret-1")]
    msg = server.sent[0]
    assert msg["From"] == "hub@example.com" and msg["To"] == "me@example.com, you@example.org" and msg["X-Priority"] == "1"
    assert msg["Subject"].startswith("[ALARM] Model Hub: Voron") and "\n" not in msg["Subject"] and "\r" not in msg["Subject"] and msg["Bcc"] is None
    assert msg.get_content().strip() == "It is done"
    with Session(engine) as s:
        channels._email(s, "t", "m", b"jpeg-bytes", "silent")
    quiet = Smtp.instances[1].sent[0]
    assert quiet["X-Priority"] == "5" and not quiet["Subject"].startswith("[ALARM]") and any(p.get_filename() == "snapshot.jpg" for p in quiet.iter_attachments())


def test_ssl_and_plain_connections_and_the_password_rule(authed, tidy, smtp, monkeypatch):
    _mail(authed, smtp_security="ssl", smtp_port="")
    with Session(engine) as s:
        assert channels._email(s, "t", "m", None, "normal") is True
    assert Smtp.instances[0].port == 465 and Smtp.instances[0].ssl and "starttls" not in Smtp.instances[0].log
    monkeypatch.setattr(socket, "gethostbyname", lambda host: {"mail.lan": "192.168.1.9", "smtp.example.com": "93.184.216.34"}.get(host, "0.0.0.0"))
    _mail(authed, smtp_security="none", smtp_port="")
    with Session(engine) as s:
        assert channels._email(s, "t", "m", None, "normal") is False                                           # a password must not cross the internet unencrypted
    assert len(Smtp.instances) == 1
    authed.put("/api/settings", json={"smtp_host": "mail.lan"})
    with Session(engine) as s:
        assert channels._email(s, "t", "m", None, "normal") is True
    assert Smtp.instances[1].port == 25 and Smtp.instances[1].log == [("login", "hubuser", "pw-secret-1")]
    authed.put("/api/settings", json={"smtp_port": "many"})
    with Session(engine) as s:
        assert channels._email(s, "t", "m", None, "normal") is False


def test_a_mail_server_that_fails_does_not_stop_the_others(authed, tidy, monkeypatch, hook):
    import smtplib

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(smtplib, "SMTP", boom)
    _mail(authed)
    authed.put("/api/settings", json={"notify_webhook_url": "https://ntfy.sh/t"})
    with Session(engine) as s:
        assert channels.send_all(s, "t", "m") == []
        assert notify.notify(s, "Model Hub: x", "y", hold=False) is True and len(hook.calls) == 1               # the webhook still got it


def test_the_mail_password_is_a_secret_and_the_test_button_reaches_e_mail(authed, tidy, smtp):
    _mail(authed)
    assert authed.get("/api/settings").json()["smtp_password"] == "********"
    assert b"pw-secret-1" not in authed.get("/api/backup").content
    r = authed.post("/api/settings/notify-test")
    assert r.status_code == 200 and r.json()["channels"] == ["email"] and len(Smtp.instances) == 1


# ---------------------------------------------------------------- out of service
def test_a_printer_out_of_service_is_skipped_and_asks_before_a_start(authed, farm):
    fake, a, b, m, _ = farm
    assert authed.patch(f"/api/printers/{a['id']}", json={"out_of_service": "yes"}).status_code == 400
    _keep(authed, m)
    item = _queue(authed, farm)
    both = authed.get("/api/queue/suggestions").json()[str(item["id"])]
    assert {c["printer_id"] for c in both} == {a["id"], b["id"]}
    assert authed.patch(f"/api/printers/{a['id']}", json={"out_of_service": True}).json()["out_of_service"] is True
    assert [c["printer_id"] for c in authed.get("/api/queue/suggestions").json()[str(item["id"])]] == [b["id"]]       # never suggested
    with Session(engine) as s:
        assert "out of service" in stagger.reason_to_wait(s, s.get(Printer, a["id"])) and stagger.reason_to_wait(s, s.get(Printer, b["id"])) is None
    fake.moonraker_state = "standby"
    mine = _queue(authed, farm, printer_id=a["id"])
    r = authed.post(f"/api/queue/{mine['id']}/send", json={"start": True})
    assert r.status_code == 409 and r.json()["detail"].startswith("Wait: ") and "out of service" in r.json()["detail"]
    assert authed.post(f"/api/queue/{mine['id']}/send", json={"start": False}).status_code == 200
    assert [p for p in authed.get("/api/printers").json()["printers"] if p["id"] == a["id"]][0]["out_of_service"] is True
    anon = TestClient(_app())
    authed.post("/api/settings/status-page", json={"enabled": True})
    link = authed.get("/api/settings/status-page").json()["path"]
    states = {p["name"]: p["state"] for p in anon.get(link + "/data").json()["printers"]}
    assert states["Alpha"] == "out of service"
    authed.post("/api/settings/status-page", json={"enabled": False})
    authed.put("/api/settings", json={"metrics_enabled": "true"})
    try:
        text = anon.get("/metrics").text
        assert 'modelhub_printer_out_of_service{printer="Alpha",kind="moonraker"} 1' in text and 'modelhub_printer_out_of_service{printer="Bravo",kind="moonraker"} 0' in text
    finally:
        authed.put("/api/settings", json={"metrics_enabled": ""})
    authed.patch(f"/api/printers/{a['id']}", json={"out_of_service": False})
    with Session(engine) as s:
        assert stagger.reason_to_wait(s, s.get(Printer, a["id"])) is None


# ---------------------------------------------------------------- the camera wall
def _cam_printer(c, name="Cam", **extra):
    r = c.post("/api/printers", json={"name": name, "kind": "moonraker", "url": "http://klipper.local:7125", "snapshot_url": "http://cam.local/snap", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_camera_wall_is_off_until_switched_on_and_shows_pictures_without_a_login(authed, tidy, monkeypatch):
    anon = TestClient(_app())
    assert anon.get("/wall/anything").status_code == 404
    assert authed.get("/api/settings/camera-wall").json() == {"enabled": False, "path": None}
    p, nocam = _cam_printer(authed), authed.post("/api/printers", json={"name": "Blind", "kind": "moonraker", "url": "http://klipper.local:7126"}).json()
    fetches = []
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: fetches.append(url) or b"\xff\xd8jpegbytes")
    on = authed.post("/api/settings/camera-wall", json={"enabled": True}).json()
    path = on["path"]
    assert re.fullmatch(r"/wall/[A-Za-z0-9_-]{30,}", path) and "wall_token" not in authed.get("/api/settings").json()
    page = anon.get(path)
    assert page.status_code == 200 and "Cameras" in page.text and page.headers["cache-control"] == "no-store" and "noindex" in page.headers["x-robots-tag"]
    data = anon.get(path + "/data").json()["printers"]
    by = {d["name"]: d for d in data}
    assert by["Cam"]["camera"] is True and by["Blind"]["camera"] is False and set(by["Cam"]) == {"id", "name", "camera", "online", "state", "progress"}
    assert "klipper.local" not in json.dumps(data) and "cam.local" not in json.dumps(data)                          # no address ever
    img = anon.get(f"{path}/cam/{p['id']}.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content == b"\xff\xd8jpegbytes"
    anon.get(f"{path}/cam/{p['id']}.jpg")
    anon.get(f"{path}/cam/{p['id']}.jpg")
    assert fetches == ["http://cam.local/snap"]                                                                      # many viewers, one fetch
    assert anon.get(f"{path}/cam/{nocam['id']}.jpg").status_code == 404 and anon.get(f"{path}/cam/999999.jpg").status_code == 404
    assert anon.get(f"/wall/wrong-{path[6:]}/cam/{p['id']}.jpg").status_code == 404 and anon.get(f"/wall/{path[6:]}x/data").status_code == 404
    assert authed.post("/api/settings/camera-wall", json={"enabled": "yes"}).status_code == 400
    new = authed.post("/api/settings/camera-wall", json={"rotate": True}).json()["path"]
    assert new != path and anon.get(path + "/data").status_code == 404 and anon.get(new + "/data").status_code == 200
    authed.post("/api/settings/camera-wall", json={"enabled": False})
    assert anon.get(new + "/data").status_code == 404 and authed.get("/api/settings/camera-wall").json()["enabled"] is False


def test_a_camera_that_does_not_answer_gives_a_bad_gateway_and_a_picture_expires(authed, tidy, monkeypatch):
    p = _cam_printer(authed)
    path = authed.post("/api/settings/camera-wall", json={"enabled": True}).json()["path"]
    anon = TestClient(_app())

    def broken(url):
        raise printing.PrinterError("The camera did not answer in time")
    monkeypatch.setattr(printing, "fetch_snapshot", broken)
    assert anon.get(f"{path}/cam/{p['id']}.jpg").status_code == 502
    calls = []
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: calls.append(1) or b"jpg")
    camera_wall._cache.clear()
    with Session(engine) as s:
        row = s.get(Printer, p["id"])
        assert camera_wall.picture(s, row, now=100.0) == b"jpg" and camera_wall.picture(s, row, now=101.9) == b"jpg" and len(calls) == 1
        assert camera_wall.picture(s, row, now=102.1) == b"jpg" and len(calls) == 2                                  # older than two seconds: asked again
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: b"x" * (camera_wall.MAX_PICTURE + 1))
    camera_wall._cache.clear()
    assert anon.get(f"{path}/cam/{p['id']}.jpg").status_code == 502                                                  # a picture that is too large is refused


# ---------------------------------------------------------------- temperature history
def test_only_a_printing_or_hot_printer_is_read_and_at_most_once_a_minute(authed, tidy):
    p = authed.post("/api/printers", json={"name": "Hot", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    temps.reset()
    with Session(engine) as s:
        idle = {"online": True, "state": "standby", "nozzle": 24.0, "bed": 23.0}
        assert temps.record(s, p["id"], idle, now=1000.0) is False
        assert temps.record(s, p["id"], {"online": False, "state": "offline"}, now=1000.0) is False
        assert temps.record(s, p["id"], {"online": True, "state": "standby", "nozzle": 24, "bed": 60.0}, now=1000.0) is True          # cooling down still counts
        assert temps.record(s, p["id"], {"online": True, "state": "printing", "nozzle": 210.0, "bed": 60.0}, now=1030.0) is False      # too soon
        assert temps.record(s, p["id"], {"online": True, "state": "printing", "nozzle": 210.0, "bed": 60.0, "chamber": 38.0}, now=1060.0) is True
        assert temps.record(s, p["id"], {"online": True, "state": "printing", "nozzle": None, "bed": None}, now=1200.0) is True
    out = authed.get(f"/api/printers/{p['id']}/temps", params={"hours": 1}).json()
    assert out["hours"] == 1 and len(out["samples"]) == 3
    assert out["samples"][1] == {"t": out["samples"][1]["t"], "nozzle": 210.0, "bed": 60.0, "chamber": 38.0} and out["samples"][1]["t"].endswith("Z")
    assert authed.get(f"/api/printers/{p['id']}/temps", params={"hours": 500}).json()["hours"] == 24 and authed.get(f"/api/printers/{p['id']}/temps", params={"hours": 0}).json()["hours"] == 0.25
    assert authed.get("/api/printers/999999/temps").status_code == 404
    authed.delete(f"/api/printers/{p['id']}")
    with Session(engine) as s:
        assert s.exec(select(TempSample)).all() == []                                                                # a later printer with that id starts clean


def test_old_readings_are_dropped_and_a_long_history_is_thinned(authed, tidy):
    p = authed.post("/api/printers", json={"name": "Old", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    temps.reset()
    with Session(engine) as s:
        s.add(TempSample(printer_id=p["id"], at=datetime.utcnow() - timedelta(hours=30), nozzle=200))
        for n in range(500):
            s.add(TempSample(printer_id=p["id"], at=datetime.utcnow() - timedelta(minutes=n), nozzle=200 + n % 5))
        s.commit()
        assert temps.record(s, p["id"], {"online": True, "state": "printing", "nozzle": 1, "bed": 1}, now=5000.0) is True       # the pruning happens with a reading
        assert not any(r.at < datetime.utcnow() - timedelta(hours=temps.KEEP_HOURS) for r in s.exec(select(TempSample)).all())
    samples = authed.get(f"/api/printers/{p['id']}/temps", params={"hours": 24}).json()["samples"]
    assert len(samples) == temps.MAX_POINTS and samples == sorted(samples, key=lambda x: x["t"])


def test_a_poll_keeps_a_reading_and_a_backup_does_not_carry_them(authed, farm):
    fake, a, b, m, _ = farm
    fake.moonraker_state = "printing"
    printwatch.reset()
    with Session(engine) as s:
        printwatch.poll(s)
        assert len(s.exec(select(TempSample).where(TempSample.printer_id == a["id"])).all()) == 1
    z = zipfile.ZipFile(io.BytesIO(authed.get("/api/backup").content))
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        z.extract("modelhub.db", d)
        db = sqlite3.connect(f"{d}/modelhub.db")
        assert db.execute("select count(*) from tempsample").fetchone()[0] == 0 and db.execute("select count(*) from heldnotice").fetchone()[0] == 0
        db.close()
    with Session(engine) as s:
        for r in s.exec(select(TempSample)).all():
            s.delete(r)
        s.commit()


# ---------------------------------------------------------------- finding printers
def test_only_private_small_ranges_are_scanned():
    assert discovery.parse_range("192.168.1.0/24")[0] == "192.168.1.1" and len(discovery.parse_range("192.168.1.0/24")) == 254
    assert discovery.parse_range("10.0.0.5") == ["10.0.0.5"] and len(discovery.parse_range("172.16.0.0/28")) == 14 and len(discovery.parse_range("192.168.7.8/31")) == 2
    for bad, why in (("8.8.8.0/24", "private"), ("192.168.0.0/16", "at most"), ("10.0.0.0/8", "at most"), ("127.0.0.0/30", "private"), ("169.254.1.0/24", "private"),
                     ("224.0.0.0/24", "private"), ("fe80::/64", "IPv4"), ("junk", "range like"), ("", "range like"), ("192.168.1.0/24; rm -rf", "range like")):
        with pytest.raises(discovery.ScanError, match=why):
            discovery.parse_range(bad)


def test_the_scan_lists_what_answers_and_marks_what_is_already_added(authed, tidy, monkeypatch):
    authed.post("/api/printers", json={"name": "Known", "kind": "moonraker", "url": "http://192.168.50.12:7125"})
    answers = {"192.168.50.12": {"ip": "192.168.50.12", "kind": "moonraker", "url": "http://192.168.50.12:7125", "name": "voron", "note": "Klipper (Moonraker)"},
               "192.168.50.20": {"ip": "192.168.50.20", "kind": "octoprint", "url": "http://192.168.50.20", "name": "192.168.50.20", "note": "OctoPrint"}}
    monkeypatch.setattr(discovery, "probe", lambda ip: answers.get(ip))
    r = authed.post("/api/printers/discover", json={"range": "192.168.50.0/24"}).json()
    assert r["scanned"] == 254 and [f["ip"] for f in r["found"]] == ["192.168.50.12", "192.168.50.20"] and [f["added"] for f in r["found"]] == [True, False]
    assert authed.post("/api/printers/discover", json={"range": "8.8.8.0/24"}).status_code == 400 and authed.post("/api/printers/discover", json={}).status_code == 400


def test_a_probe_recognises_klipper_octoprint_and_bambu_ports(monkeypatch):
    open_ports = {("10.0.0.1", 7125), ("10.0.0.2", 80), ("10.0.0.3", 8883), ("10.0.0.3", 990), ("10.0.0.4", 80), ("10.0.0.5", 8883)}
    monkeypatch.setattr(discovery, "_tcp_open", lambda ip, port, timeout=0: (ip, port) in open_ports)
    replies = {"http://10.0.0.1:7125/server/info": (200, {"result": {"klippy_state": "ready"}}), "http://10.0.0.1:7125/printer/info": (200, {"result": {"hostname": "voron"}}),
               "http://10.0.0.2:80/api/version": (200, {"server": "1.9.3", "text": "OctoPrint 1.9.3"}), "http://10.0.0.4:80/api/version": (200, {"hello": "a router"})}
    monkeypatch.setattr(discovery, "_get_json", lambda url: replies.get(url, (None, None)))
    assert discovery.probe("10.0.0.1") == {"ip": "10.0.0.1", "kind": "moonraker", "url": "http://10.0.0.1:7125", "name": "voron", "note": "Klipper (Moonraker)"}
    assert discovery.probe("10.0.0.2")["kind"] == "octoprint" and discovery.probe("10.0.0.2")["url"] == "http://10.0.0.2"
    bambu = discovery.probe("10.0.0.3")
    assert bambu["kind"] == "bambu" and "serial number" in bambu["note"]
    assert discovery.probe("10.0.0.4") is None and discovery.probe("10.0.0.5") is None and discovery.probe("10.0.0.9") is None               # a router, one port only, nothing


# ---------------------------------------------------------------- diagnosing one
def test_a_diagnosis_stops_at_the_first_real_problem(authed, tidy, monkeypatch):
    p = authed.post("/api/printers", json={"name": "Diag", "kind": "moonraker", "url": "http://klipper.local:7125", "snapshot_url": "http://cam.local/snap"}).json()
    r = authed.post(f"/api/printers/{p['id']}/diagnose").json()
    assert r["steps"][0]["ok"] is False and "does not resolve" in r["steps"][0]["detail"] and len(r["steps"]) == 1
    monkeypatch.setattr(socket, "gethostbyname", lambda host: "192.168.1.60")
    monkeypatch.setattr(discovery, "_tcp_open", lambda ip, port, timeout=0: False)
    r = authed.post(f"/api/printers/{p['id']}/diagnose").json()
    assert [s["step"] for s in r["steps"]] == ["Address", "Port"] and "7125" in r["steps"][1]["detail"] and "cannot be reached" in r["summary"]
    monkeypatch.setattr(discovery, "_tcp_open", lambda ip, port, timeout=0: True)
    monkeypatch.setattr(printing, "status", lambda *a, **k: {"online": False, "state": "offline", "message": "The printer answered with an error (401)"})
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: (_ for _ in ()).throw(printing.PrinterError("The camera did not give a picture")))
    r = authed.post(f"/api/printers/{p['id']}/diagnose").json()
    assert [s["ok"] for s in r["steps"]] == [True, True, False, False] and "API key" in r["steps"][2]["detail"] and r["summary"] == r["steps"][2]["detail"]
    monkeypatch.setattr(printing, "status", lambda *a, **k: {"online": True, "state": "standby", "nozzle": 24.0})
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: b"x" * 4096)
    r = authed.post(f"/api/printers/{p['id']}/diagnose").json()
    assert all(s["ok"] for s in r["steps"]) and r["summary"].startswith("Everything") and "4 KB" in r["steps"][3]["detail"]
    assert authed.post("/api/printers/999999/diagnose").status_code == 404


# ---------------------------------------------------------------- the log and the support bundle
def test_secrets_are_blanked_in_log_lines():
    for text, gone in (("GET /x?token=abc12345secret&y=1", "abc12345secret"), ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"), ("using mh_AbCdEfGh1234567890 here", "AbCdEfGh1234567890"),
                       ("login password=hunter22 ok", "hunter22"), ("see /status/AbCdEfGhIjKlMnOpQrStUvWx now", "AbCdEfGhIjKlMnOpQrStUvWx"), ("hook /hooks/shop/AbCdEfGhIjKlMnOpQrStUvWx", "AbCdEfGhIjKlMnOpQrStUvWx"),
                       ("fetch http://user:pass12@host/x", "pass12"), ("/quote/AbCdEfGhIjKlMnOpQrStUvWx", "AbCdEfGhIjKlMnOpQrStUvWx")):
        assert gone not in logbuffer.redact(text), text
    assert logbuffer.redact("nothing secret here, 3 printers") == "nothing secret here, 3 printers"


def test_the_log_can_be_read_by_the_administrator_only(authed, tidy):
    import logging
    logging.getLogger("modelhub.test").warning("a visible warning %s", "token=SECRETVALUE123")
    logging.getLogger("modelhub.test").info("just info")
    lines = authed.get("/api/system/logs", params={"level": "WARNING", "q": "visible"}).json()["lines"]
    assert len(lines) == 1 and lines[0]["level"] == "WARNING" and "SECRETVALUE123" not in lines[0]["message"] and "token=***" in lines[0]["message"]
    assert any(l["message"] == "just info" for l in authed.get("/api/system/logs", params={"level": "INFO", "limit": 1000}).json()["lines"])
    assert authed.get("/api/system/logs", params={"limit": 5000}).status_code == 422
    authed.post("/api/users", json={"username": "logmember", "password": PW, "role": "member"})
    try:
        member = TestClient(_app())
        assert member.post("/api/auth/login", json={"username": "logmember", "password": PW}).status_code == 200
        for url in ("/api/system/logs", "/api/system/support-bundle.zip"):
            assert member.get(url).status_code == 403, url
        assert member.post("/api/printers/discover", json={"range": "192.168.1.0/24"}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")


def test_the_support_bundle_has_what_helps_and_nothing_secret(authed, tidy):
    authed.put("/api/settings", json={"smtp_password": "pw-should-not-appear", "notify_webhook_url": "https://hooks.example/secret-path-123"})
    authed.post("/api/printers", json={"name": "Bundle", "kind": "octoprint", "url": "http://octopi.local", "api_key": "printer-key-should-not-appear"})
    r = authed.get("/api/system/support-bundle.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and "attachment" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["info.json", "log.txt"]
    info = json.loads(z.read("info.json"))
    assert info["version"] and "smtp_password" in info["settings_that_are_set"] and "tables" in info and any(p["name"] == "Bundle" and p["has_key"] and p["host"] == "octopi.local" for p in info["printers"])
    blob = b"".join(z.read(n) for n in z.namelist())
    for secret in (b"pw-should-not-appear", b"secret-path-123", b"printer-key-should-not-appear"):
        assert secret not in blob
    assert "appsettings" not in info["tables"] and "appuser" not in info["tables"]


def test_the_controls_are_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html, js = (root / "index.html").read_text(encoding="utf-8"), (root / "app.js").read_text(encoding="utf-8")
    for element in ("quiet-start", "digest-daily", "smtp-host", "wall-box", "discover-box", "log-box"):
        assert f'id="{element}"' in html, element
    for needle in ("quiet_start", "digest_time", "smtp_host", "/api/settings/camera-wall", "/api/printers/discover", "/diagnose", "/temps", "out_of_service", "/api/system/logs", "support-bundle.zip"):
        assert needle in js or needle in html, needle
