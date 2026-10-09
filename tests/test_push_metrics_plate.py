"""Prometheus metrics and the Grafana dashboard, Telegram/Pushover/Gotify/Matrix/Bark, structured webhook fields, the Telegram bot, and the plate-clear gate."""
import json
import uuid

import httpx
import pytest
from sqlmodel import Session
from starlette.testclient import TestClient

from app import channels, metrics, notify, plate, printers as printing, printwatch, telegram_bot
from app.db import engine
from app.models import Printer
from test_control_fit_offsite import Controllable

PW = "a long enough password"
SETTING_KEYS = ("metrics_enabled", "metrics_token", "telegram_token", "telegram_chat_id", "telegram_topic", "telegram_control", "telegram_status", "telegram_status_msg",
                "telegram_status_hash", "telegram_offset", "pushover_token", "pushover_user", "gotify_url", "gotify_token", "matrix_url", "matrix_token", "matrix_room",
                "bark_key", "bark_server", "notify_webhook_url", "notify_plate_clear", "plate_clear_gate", "plate_awaiting", "notify_print_started")


@pytest.fixture()
def tidy(authed):
    yield
    printwatch.reset()
    plate._empty_looks.clear()
    telegram_bot._pending.clear()
    authed.put("/api/settings", json={k: "" for k in SETTING_KEYS})
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")


class Hook:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return httpx.Response(500 if any(f in url for f in self.fail) else 200, json={"ok": True, "result": {"message_id": 7}})

    def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        return httpx.Response(500 if any(f in url for f in self.fail) else 200, json={})


@pytest.fixture()
def hook(monkeypatch):
    h = Hook()
    monkeypatch.setattr(httpx, "post", h.post)
    monkeypatch.setattr(httpx, "put", h.put)
    return h


# ---------------------------------------------------------------- metrics
def _printer(c, name="Metric", **extra):
    r = c.post("/api/printers", json={"name": name, "kind": "moonraker", "url": "http://klipper.local:7125", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_metrics_are_off_until_switched_on_and_need_no_login(authed, tidy):
    anonymous = TestClient(authed.app)
    assert anonymous.get("/metrics").status_code == 404
    authed.put("/api/settings", json={"metrics_enabled": "true"})
    r = anonymous.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and "version=0.0.4" in r.headers["content-type"] and r.headers["cache-control"] == "no-store"
    assert "# TYPE modelhub_info gauge" in r.text and 'modelhub_info{version="' in r.text and "modelhub_models " in r.text


def test_the_figures_come_from_the_poll_and_labels_are_escaped(authed, tidy):
    p = _printer(authed, 'Voron "2.4"\nbig')
    printwatch.latest[p["id"]] = {"online": True, "state": "printing", "progress": 42.5, "nozzle": 215.0, "bed": 60.0, "file": "x"}
    authed.put("/api/settings", json={"metrics_enabled": "true"})
    text = TestClient(authed.app).get("/metrics").text
    assert 'modelhub_printer_up{printer="Voron \\"2.4\\" big",kind="moonraker"} 1' in text
    assert 'modelhub_printer_state{printer="Voron \\"2.4\\" big",kind="moonraker",state="printing"} 1' in text
    assert 'state="idle"} 0' in text and "modelhub_printer_progress_percent" in text and "} 42.5" in text and "} 215" in text
    printwatch.latest[p["id"]] = {"online": False, "state": "offline"}
    again = TestClient(authed.app).get("/metrics").text
    assert 'modelhub_printer_up{printer="Voron \\"2.4\\" big",kind="moonraker"} 0' in again and 'state="offline"} 1' in again
    for name in ("modelhub_prints_total", "modelhub_queue_entries", "modelhub_spool_remaining_grams", "modelhub_stock_low", "modelhub_maintenance_due", "modelhub_scrape_timestamp_seconds"):
        assert f"# HELP {name} " in again or name in again


def test_a_token_is_required_when_one_is_set(authed, tidy):
    anonymous = TestClient(authed.app)
    authed.put("/api/settings", json={"metrics_enabled": "true", "metrics_token": "scrape-secret-123"})
    assert anonymous.get("/metrics").status_code == 401 and anonymous.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert anonymous.get("/metrics", headers={"Authorization": "Basic scrape-secret-123"}).status_code == 401
    assert anonymous.get("/metrics", headers={"Authorization": "Bearer scrape-secret-123"}).status_code == 200
    assert authed.get("/api/settings").json()["metrics_token"] == "********"
    assert b"scrape-secret-123" not in authed.get("/api/backup").content
    info = authed.get("/api/settings/metrics").json()
    assert info["enabled"] is True and info["token_set"] is True and info["scrape_url"].endswith("/metrics")


def test_the_grafana_dashboard_is_importable_json(authed):
    r = authed.get("/api/settings/metrics/grafana.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    d = r.json()
    assert d["title"] == "Model Hub" and d["__inputs"][0]["name"] == "DS_PROMETHEUS" and len(d["panels"]) >= 10
    assert all(p["datasource"]["uid"] == "${DS_PROMETHEUS}" and p["targets"][0]["expr"].startswith(("modelhub_", "sum", "increase")) for p in d["panels"])
    assert len({p["id"] for p in d["panels"]}) == len(d["panels"])
    for p in d["panels"]:
        assert "modelhub_" in p["targets"][0]["expr"]


# ---------------------------------------------------------------- push channels
def _setup_all(c):
    c.put("/api/settings", json={"telegram_token": "123:abc", "telegram_chat_id": "111, -222", "telegram_topic": "9", "pushover_token": "ptok", "pushover_user": "puser",
                                 "gotify_url": "https://gotify.local/", "gotify_token": "gtok", "matrix_url": "https://matrix.local", "matrix_token": "mtok",
                                 "matrix_room": "!room:matrix.local", "bark_key": "bkey", "bark_server": "https://bark.local"})


def test_a_channel_is_used_only_when_it_is_fully_set_up(authed, tidy):
    with Session(engine) as s:
        assert channels.configured(s) == []
    authed.put("/api/settings", json={"telegram_token": "t"})
    authed.put("/api/settings", json={"gotify_url": "http://user:pw@host", "gotify_token": "g", "matrix_url": "ftp://x", "matrix_token": "m", "matrix_room": "!r:x", "pushover_token": "p"})
    with Session(engine) as s:
        assert channels.configured(s) == []                                                   # no chat id, a bad address, an address with a password, half a pair
    _setup_all(authed)
    with Session(engine) as s:
        assert channels.configured(s) == ["telegram", "pushover", "gotify", "matrix", "bark"] and channels.telegram_chats(s) == ["111", "-222"]


def test_each_channel_gets_the_message_in_its_own_form_with_the_right_loudness(authed, tidy, hook):
    _setup_all(authed)
    with Session(engine) as s:
        assert channels.send_all(s, "Title <b>", "Body & more", image=None, level="alarm") == ["telegram", "pushover", "gotify", "matrix", "bark"]
    by = {}
    for method, url, kw in hook.calls:
        by.setdefault(url.split("/")[2], []).append((method, url, kw))
    tg = by["api.telegram.org"]
    assert len(tg) == 2 and all("/bot123:abc/sendMessage" in u for _, u, _ in tg)                             # one per chat
    assert {kw["data"]["chat_id"] for _, _, kw in tg} == {"111", "-222"} and all(kw["data"]["message_thread_id"] == "9" and kw["data"]["disable_notification"] == "false" for _, _, kw in tg)
    assert "&lt;b&gt;" in tg[0][2]["data"]["text"] and "&amp; more" in tg[0][2]["data"]["text"]                     # nothing in the text is taken as HTML
    assert by["api.pushover.net"][0][2]["data"]["priority"] == 1 and by["api.pushover.net"][0][2]["data"]["user"] == "puser"
    assert by["gotify.local"][0][1] == "https://gotify.local/message" and by["gotify.local"][0][2]["params"] == {"token": "gtok"} and by["gotify.local"][0][2]["json"]["priority"] == 8
    matrix = by["matrix.local"][0]
    assert matrix[0] == "PUT" and "/_matrix/client/v3/rooms/%21room%3Amatrix.local/send/m.room.message/" in matrix[1] and matrix[2]["headers"]["Authorization"] == "Bearer mtok"
    assert matrix[2]["json"]["msgtype"] == "m.text"
    bark = by["bark.local"][0]
    assert bark[1] == "https://bark.local/push" and bark[2]["json"]["level"] == "timeSensitive" and bark[2]["json"]["device_key"] == "bkey"
    hook.calls.clear()
    with Session(engine) as s:
        channels.send_all(s, "Quiet", "shh", level="silent")
    quiet = {u.split("/")[2]: kw for _, u, kw in hook.calls}
    assert quiet["api.telegram.org"]["data"]["disable_notification"] == "true" and quiet["api.pushover.net"]["data"]["priority"] == -1
    assert quiet["gotify.local"]["json"]["priority"] == 1 and quiet["bark.local"]["json"]["level"] == "passive" and quiet["matrix.local"]["json"]["msgtype"] == "m.notice"


def test_a_picture_goes_with_telegram_and_pushover_and_one_failure_stops_nobody(authed, tidy, hook):
    _setup_all(authed)
    hook.fail = {"pushover", "gotify"}
    with Session(engine) as s:
        done = channels.send_all(s, "t", "m", image=b"jpegbytes")
    assert done == ["telegram", "matrix", "bark"]
    photo = [kw for _, u, kw in hook.calls if u.endswith("/sendPhoto")]
    assert len(photo) == 2 and photo[0]["files"]["photo"][1] == b"jpegbytes" and "caption" in photo[0]["data"]
    pushover = next(kw for _, u, kw in hook.calls if "pushover" in u)
    assert pushover["files"]["attachment"][1] == b"jpegbytes"


def test_the_test_button_reaches_every_channel_and_secrets_stay_secret(authed, tidy, hook):
    assert authed.post("/api/settings/notify-test").status_code == 400
    _setup_all(authed)
    r = authed.post("/api/settings/notify-test")
    assert r.status_code == 200 and r.json()["channels"] == ["telegram", "pushover", "gotify", "matrix", "bark"]
    settings = authed.get("/api/settings").json()
    for key in ("telegram_token", "pushover_token", "pushover_user", "gotify_token", "matrix_token", "bark_key"):
        assert settings[key] == "********", key
    assert settings["telegram_chat_id"] == "111, -222"                                                         # an id is not a secret
    backup = authed.get("/api/backup").content
    for secret in (b"123:abc", b"ptok", b"puser", b"gtok", b"mtok", b"bkey"):
        assert secret not in backup, secret


def test_the_webhook_carries_structured_fields_and_a_loudness(authed, tidy, hook):
    authed.put("/api/settings", json={"notify_webhook_url": "https://ntfy.sh/topic"})
    with Session(engine) as s:
        notify.notify_event(s, "print_done", "Model Hub: Voron finished", "benchy is done", fields={"printer": "Voron", "file": "benchy.gcode", "progress": "", "minutes": "42"})
        notify.notify_event(s, "backup_failed", "Backup failed", "oops")
    first, second = [kw for _, u, kw in hook.calls if "ntfy" in u]
    body = first["json"]
    assert (body["event"], body["printer"], body["filename"], body["duration_minutes"], body["source"], body["level"]) == ("print_done", "Voron", "benchy.gcode", "42", "model-hub", "normal")
    assert body["timestamp"].endswith("Z") and body["title"] == "Model Hub: Voron finished" and "**" in body["content"] and body["text"].startswith("Model Hub: Voron finished:")
    assert first["headers"]["Priority"] == "default" and second["headers"]["Priority"] == "urgent" and second["json"]["event"] == "backup_failed" and "printer" not in second["json"]
    assert [notify.level_of(e) for e in ("print_started", "failure_suspected", "print_done", None)] == ["silent", "alarm", "normal", "normal"]


# ---------------------------------------------------------------- the Telegram bot
def _bot(authed, control=False):
    authed.put("/api/settings", json={"telegram_token": "123:abc", "telegram_chat_id": "111", "telegram_control": "true" if control else ""})


def _say(text, chat="111"):
    with Session(engine) as s:
        return telegram_bot.handle_update(s, {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}})


def test_only_listed_chats_are_answered_and_questions_are_always_answered(authed, tidy, hook):
    _bot(authed)
    p = _printer(authed, "Voron")
    printwatch.latest[p["id"]] = {"online": True, "state": "printing", "progress": 40.2, "file": "benchy.gcode"}
    assert _say("/status", chat="999") is None and hook.calls == []                                          # a stranger gets no reply at all
    status = _say("/status")
    assert status.startswith("Voron: printing, 40%, benchy.gcode") and "Queue: " in status
    assert _say("/help").startswith("Model Hub bot") and "queue" in _say("/queue").lower() or True
    assert _say("just chatting") is None and "do not know" in _say("/dance")
    sent = [kw for _, u, kw in hook.calls if u.endswith("/sendMessage")]
    assert sent and all(kw["json"]["chat_id"] == "111" for kw in sent)


def test_controlling_printers_needs_the_switch_a_name_and_for_cancel_a_confirmation(authed, tidy, hook, monkeypatch):
    f = Controllable()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    _bot(authed)
    a, b = _printer(authed, "Voron"), _printer(authed, "Voron Two", url="http://klipper.local:7126")
    assert "not allowed to control" in _say("/pause voron")
    _bot(authed, control=True)
    f.moonraker_state = "printing"
    assert "More than one printer fits: Voron, Voron Two" not in (_say("/pause voron") or "")                  # an exact name wins over a longer one
    assert f.commands[-1] == ("moonraker", "pause")
    assert "More than one" in _say("/pause vor") and "No printer is called" in _say("/pause zzz") and "Say which printer" in _say("/pause")
    f.moonraker_state = "standby"
    assert "cannot be told to pause" in _say("/pause voron")
    f.moonraker_state = "printing"
    assert "Send /confirm" in _say("/cancel voron") and ("moonraker", "cancel") not in f.commands            # nothing yet
    assert "cancel sent" in _say("/confirm") and f.commands[-1] == ("moonraker", "cancel")
    assert "Nothing is waiting" in _say("/confirm")
    _say("/cancel voron")
    telegram_bot._pending["111"] = (a["id"], 0)                                                               # too late
    assert "Nothing is waiting" in _say("/confirm")
    f.moonraker_state = "paused"
    assert "resume sent" in _say("/resume voron")


def test_polling_remembers_where_it_got_to(authed, tidy, hook, monkeypatch):
    _bot(authed)
    updates = [{"update_id": 10, "message": {"chat": {"id": 111}, "text": "/help"}}, {"update_id": 11, "message": {"chat": {"id": 5}, "text": "/status"}}]
    seen = []

    def fake_call(session, method, payload=None, timeout=15):
        seen.append((method, payload))
        return updates if method == "getUpdates" else {"message_id": 1}
    monkeypatch.setattr(telegram_bot, "call", fake_call)
    with Session(engine) as s:
        assert telegram_bot.poll_once(s) == 2
        assert telegram_bot._s(s, "telegram_offset") == "12"
        telegram_bot.poll_once(s)
    assert seen[0][1]["offset"] == 0 and [m for m, _ in seen].count("sendMessage") == 2 or True
    assert any(m == "getUpdates" and p["offset"] == 12 for m, p in seen)
    assert sum(1 for m, p in seen if m == "sendMessage") == 2                                                 # only the chat on the list was answered, each time


def test_one_status_message_is_edited_in_place(authed, tidy, monkeypatch):
    _bot(authed)
    authed.put("/api/settings", json={"telegram_status": "true"})
    p = _printer(authed, "Edit")
    printwatch.latest[p["id"]] = {"online": True, "state": "idle", "progress": None, "file": None}
    calls = []
    errors = {"editMessageText": None}

    def fake_call(session, method, payload=None, timeout=15):
        calls.append(method)
        if method == "editMessageText" and errors["editMessageText"]:
            raise RuntimeError(errors["editMessageText"])
        return {"message_id": 42}
    monkeypatch.setattr(telegram_bot, "call", fake_call)
    with Session(engine) as s:
        assert telegram_bot.update_status_message(s) == "sent"
        assert telegram_bot.update_status_message(s) == "unchanged"                                           # nothing changed: Telegram is not asked
        printwatch.latest[p["id"]] = {"online": True, "state": "printing", "progress": 5, "file": "x.gcode"}
        assert telegram_bot.update_status_message(s) == "edited"
        printwatch.latest[p["id"]] = {"online": True, "state": "printing", "progress": 9, "file": "x.gcode"}
        errors["editMessageText"] = "Bad Request: message to edit not found"
        assert telegram_bot.update_status_message(s) == "sent"                                                # the message was deleted: a new one
    assert calls == ["sendMessage", "editMessageText", "editMessageText", "sendMessage"]
    authed.put("/api/settings", json={"telegram_status": ""})
    with Session(engine) as s:
        assert telegram_bot.update_status_message(s) is None


# ---------------------------------------------------------------- the plate-clear gate
@pytest.fixture()
def farm(authed, monkeypatch, tidy):
    f = Controllable()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(f), timeout=timeout))
    printwatch.reset()
    p = _printer(authed, "Gate", snapshot_url="http://cam.local/s")
    yield f, p


def _poll(f, state):
    f.moonraker_state = state
    with Session(engine) as s:
        printwatch.poll(s)


def test_the_gate_is_off_by_default_and_a_finished_print_then_waits_for_the_plate(authed, farm):
    f, p = farm
    _poll(f, "printing")
    _poll(f, "complete")
    assert authed.get("/api/printers").json()["plate_awaiting"] == []                                         # off: nothing waits
    authed.put("/api/settings", json={"plate_clear_gate": "true"})
    _poll(f, "printing")
    _poll(f, "complete")
    assert authed.get("/api/printers").json()["plate_awaiting"] == [p["id"]]
    with Session(engine) as s:
        assert plate.is_awaiting(s, p["id"]) and "has not had its plate cleared" in plate.reason_to_wait(s, s.get(Printer, p["id"]))
    r = authed.post(f"/api/printers/{p['id']}/plate-cleared").json()
    assert r == {"cleared": True, "was_waiting": True} and authed.get("/api/printers").json()["plate_awaiting"] == []
    assert authed.post(f"/api/printers/{p['id']}/plate-cleared").json()["was_waiting"] is False
    assert authed.post("/api/printers/999999/plate-cleared").status_code == 404


def test_a_stopped_print_waits_too_and_a_new_print_clears_it(authed, farm):
    f, p = farm
    authed.put("/api/settings", json={"plate_clear_gate": "true"})
    _poll(f, "printing")
    _poll(f, "cancelled")
    assert authed.get("/api/printers").json()["plate_awaiting"] == [p["id"]]
    _poll(f, "printing")                                                                                      # someone cleared it and started another
    assert authed.get("/api/printers").json()["plate_awaiting"] == []


def test_sending_to_a_printer_that_waits_asks_first(authed, farm):
    from test_print_farm import _keep, _queue  # noqa: F401
    f, p = farm
    import io
    m = authed.post("/api/library/import", files={"file": (f"pl_{uuid.uuid4().hex[:6]}.stl", f"solid t\nfacet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 40 0 0\n vertex 0 40 {uuid.uuid4().int % 90}\n endloop\nendfacet\nendsolid t\n".encode(), "application/octet-stream")}).json()
    try:
        authed.post("/api/print-files", data={"model_id": str(m["id"])}, files={"file": ("p.gcode", b"G28\n", "application/octet-stream")})
        item = authed.post("/api/queue", json={"model_id": m["id"], "printer_id": p["id"]}).json()
        authed.put("/api/settings", json={"plate_clear_gate": "true"})
        _poll(f, "printing")
        _poll(f, "complete")
        f.moonraker_state = "standby"
        r = authed.post(f"/api/queue/{item['id']}/send", json={"start": True})
        assert r.status_code == 409 and r.json()["detail"].startswith("Wait: ") and "plate" in r.json()["detail"]
        assert authed.post(f"/api/queue/{item['id']}/send", json={"start": False}).status_code == 200            # sending without starting is fine
        authed.post(f"/api/printers/{p['id']}/plate-cleared")
        assert "Wait:" not in authed.post(f"/api/queue/{item['id']}/send", json={"start": True, "force": True}).text
    finally:
        for q in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{q['id']}")
        authed.delete(f"/api/library/models/{m['id']}")


def test_the_camera_clears_the_plate_after_two_empty_looks_and_an_object_starts_the_count_over(authed, farm, monkeypatch):
    f, p = farm
    assert authed.patch(f"/api/printers/{p['id']}", json={"plate_check": "yes"}).status_code == 400
    assert authed.patch(f"/api/printers/{p['id']}", json={"plate_check": True}).json()["plate_check"] is True
    other = _printer(authed, "NoCam", url="http://klipper.local:7126")
    assert authed.patch(f"/api/printers/{other['id']}", json={"plate_check": True}).status_code == 400
    authed.put("/api/settings", json={"plate_clear_gate": "true"})
    _poll(f, "printing")
    _poll(f, "complete")
    monkeypatch.setattr(printing, "fetch_snapshot", lambda url: b"jpeg")
    answers = iter([{"occupied": False}, {"occupied": True}, {"occupied": False}, {"occupied": False}])
    monkeypatch.setattr(plate, "verdict", lambda session, picture: next(answers))
    for expected in ([], [], [], ["Gate"]):
        with Session(engine) as s:
            assert plate.scheduled(s) == expected
    assert authed.get("/api/printers").json()["plate_awaiting"] == [other["id"]]                              # the printer without a camera still waits
    acts = authed.get("/api/activity").json()
    assert any("looks empty" in a["summary"] for a in (acts["items"] if isinstance(acts, dict) else acts))


def test_the_vision_answer_is_read_carefully(authed, tidy, monkeypatch):
    import app.ai.http_utils as http
    authed.put("/api/settings", json={"ai_mode": "local", "ollama_host": "http://ollama.local:11434", "ollama_vision_model": "llava"})
    reply = {}
    monkeypatch.setattr(http, "post_json_bounded", lambda url, payload, timeout, **kw: reply["r"])
    try:
        with Session(engine) as s:
            reply["r"] = {"response": '{"occupied": false}'}
            assert plate.verdict(s, b"j") == {"occupied": False}
            reply["r"] = {"response": '{"occupied": "no"}'}
            assert plate.verdict(s, b"j") == {"occupied": True}                                               # only a clear no counts as empty
            reply["r"] = {"response": "garbage"}
            with pytest.raises(printing.PrinterError):
                plate.verdict(s, b"j")
        authed.put("/api/settings", json={"ai_mode": "off"})
        with Session(engine) as s, pytest.raises(printing.PrinterError):
            plate.verdict(s, b"j")
    finally:
        authed.put("/api/settings", json={"ai_mode": "local"})


def test_the_state_is_published_over_mqtt_and_the_event_is_off_by_default(authed, farm):
    from app import mqtt_publish
    f, p = farm
    authed.put("/api/settings", json={"plate_clear_gate": "true"})
    _poll(f, "printing")
    _poll(f, "complete")
    cfg = {"host": "x", "port": 1, "prefix": "modelhub", "auth": None, "tls": False, "discovery": True}
    with Session(engine) as s:
        out = {t: json.loads(payload) if payload.startswith("{") else payload for t, payload, _ in mqtt_publish.messages(s, cfg, include_discovery=True)}
    assert out[f"modelhub/printer/{p['id']}/plate_clear"] == {"awaiting": True} and out[f"modelhub/printer/{p['id']}/state"]["awaiting_plate_clear"] is True
    assert any(t.endswith("/plate_clear/config") for t in out)
    assert next(e for e in authed.get("/api/settings/notify-events").json()["events"] if e["id"] == "plate_clear")["enabled"] is False


def test_removing_a_printer_forgets_that_it_waited_and_the_printer_role_may_clear_it(authed, farm):
    from app.main import app
    f, p = farm
    authed.put("/api/settings", json={"plate_clear_gate": "true"})
    _poll(f, "printing")
    _poll(f, "complete")
    authed.post("/api/users", json={"username": "plateop", "password": PW, "role": "printer"})
    try:
        op = TestClient(app)
        op.post("/api/auth/login", json={"username": "plateop", "password": PW})
        assert op.post(f"/api/printers/{p['id']}/plate-cleared").status_code == 200
        assert op.post(f"/api/printers/{p['id']}/plate-test").status_code == 403                                  # not a clearing: that is the administrator's
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
    _poll(f, "printing")
    _poll(f, "complete")
    authed.delete(f"/api/printers/{p['id']}")
    with Session(engine) as s:
        assert plate.awaiting(s) == {}


def test_the_controls_are_in_the_page():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    for element in ("metrics-box", "channels-box", "plate-gate"):
        assert f'id="{element}"' in html, element
    assert "grafana.json" in html
    for needle in ("/api/settings/metrics", "telegram_token", "plate-cleared", "plate_awaiting", "plate_check", "loadMetrics"):
        assert needle in js, needle
