"""Publishing printer state and library numbers to MQTT, with Home Assistant discovery."""
import io
import json
import zipfile

import httpx
import pytest
from starlette.testclient import TestClient

from app import mqtt_publish, printwatch, scheduler
from app import printers as printing
from test_printers import FakePrinter

PW = "a long enough password"


@pytest.fixture(autouse=True)
def clean(authed, monkeypatch):
    sent = []
    monkeypatch.setattr(mqtt_publish, "_send", lambda messages, cfg: sent.append((list(messages), cfg)))
    mqtt_publish.reset()
    printwatch.reset()
    yield sent
    authed.put("/api/settings", json={k: "" for k in ("mqtt_host", "mqtt_port", "mqtt_user", "mqtt_password", "mqtt_prefix", "mqtt_discovery", "mqtt_tls", "mqtt_last_error")})


def _configure(c, **extra):
    c.put("/api/settings", json={"mqtt_host": "broker.local", **extra})


def _session():
    from sqlmodel import Session
    from app.db import engine
    return Session(engine)


def _topics(sent, run=-1):
    return {t: json.loads(p) if p.startswith(("{", "[")) else p for t, p, r in sent[run][0]}


def test_nothing_is_published_until_a_broker_is_set(authed, clean):
    with _session() as s:
        assert mqtt_publish.settings(s) is None and mqtt_publish.publish(s) == 0
    assert clean == []


def test_settings_are_read_with_defaults_and_sanitised(authed):
    _configure(authed)
    with _session() as s:
        cfg = mqtt_publish.settings(s)
    assert cfg == {"host": "broker.local", "port": 1883, "prefix": "modelhub", "auth": None, "tls": False, "discovery": True}
    _configure(authed, mqtt_port="8883", mqtt_user="ha", mqtt_password="pw", mqtt_prefix="/home lab/models!/", mqtt_tls="true", mqtt_discovery="false")
    with _session() as s:
        cfg = mqtt_publish.settings(s)
    assert cfg["port"] == 8883 and cfg["auth"] == {"username": "ha", "password": "pw"} and cfg["tls"] is True and cfg["discovery"] is False
    assert cfg["prefix"] == "homelab/models"
    _configure(authed, mqtt_port="not a port")
    with _session() as s:
        assert mqtt_publish.settings(s)["port"] == 1883


@pytest.fixture()
def printer(authed, monkeypatch):
    fake = FakePrinter()
    monkeypatch.setattr(printing, "_client", lambda timeout=printing.TIMEOUT: httpx.Client(transport=httpx.MockTransport(fake), timeout=timeout))
    p = authed.post("/api/printers", json={"name": "Voron 2.4", "kind": "moonraker", "url": "http://klipper.local:7125"}).json()
    yield fake, p
    authed.delete(f"/api/printers/{p['id']}")


def test_printer_state_and_stats_are_published_from_the_existing_poll(authed, clean, printer):
    fake, p = printer
    _configure(authed)
    with _session() as s:
        printwatch.poll(s)
        count = mqtt_publish.publish(s)
    assert count > 10                                                           # discovery + state + stats
    topics = _topics(clean)
    state = topics[f"modelhub/printer/{p['id']}/state"]
    assert state == {"name": "Voron 2.4", "online": True, "state": "printing", "progress": 42.4, "file": "benchy.gcode", "nozzle": 215.0, "bed": 59.9}
    stats = topics["modelhub/stats"]
    assert set(stats) == {"models", "never_printed", "prints_this_month", "low_stock", "filament_remaining_g", "queue_waiting", "update_available"}
    assert all(retain for _, _, retain in clean[-1][0])


def test_a_printer_that_has_not_been_polled_yet_is_skipped(authed, clean, printer):
    fake, p = printer
    _configure(authed)
    with _session() as s:
        mqtt_publish.publish(s)
    assert f"modelhub/printer/{p['id']}/state" not in _topics(clean)


def test_home_assistant_discovery_is_well_formed(authed, clean, printer):
    fake, p = printer
    _configure(authed)
    with _session() as s:
        printwatch.poll(s)
        mqtt_publish.publish(s)
    topics = _topics(clean)
    cfg = topics[f"homeassistant/sensor/modelhub_printer_{p['id']}/state/config"]
    assert cfg["state_topic"] == f"modelhub/printer/{p['id']}/state" and cfg["value_template"] == "{{ value_json.state }}"
    assert cfg["device"] == {"identifiers": [f"modelhub_printer_{p['id']}"], "name": "Voron 2.4", "manufacturer": "Model Hub"}
    assert topics[f"homeassistant/sensor/modelhub_printer_{p['id']}/nozzle/config"]["unit_of_measurement"] == "°C"
    assert topics[f"homeassistant/binary_sensor/modelhub_printer_{p['id']}/online/config"]["device_class"] == "connectivity"
    assert topics["homeassistant/sensor/modelhub_library/models/config"]["state_topic"] == "modelhub/stats"
    ids = [c["unique_id"] for t, c in topics.items() if t.startswith("homeassistant/") and isinstance(c, dict)]
    assert len(ids) == len(set(ids))                                           # no clashing entities


def test_discovery_is_sent_once_then_again_when_printers_change_or_after_an_hour(authed, clean, printer, monkeypatch):
    fake, p = printer
    _configure(authed)
    with _session() as s:
        printwatch.poll(s)
        mqtt_publish.publish(s)
        mqtt_publish.publish(s)
        assert any(t.startswith("homeassistant/") for t in _topics(clean, 0)) and not any(t.startswith("homeassistant/") for t in _topics(clean, 1))
        extra = authed.post("/api/printers", json={"name": "Second", "kind": "moonraker", "url": "http://klipper.local:7126"}).json()
        try:
            mqtt_publish.publish(s)
            assert any(t.startswith("homeassistant/") for t in _topics(clean, 2))
            mqtt_publish.publish(s)
            assert not any(t.startswith("homeassistant/") for t in _topics(clean, 3))
            monkeypatch.setattr(mqtt_publish.time, "time", lambda: mqtt_publish._discovery["at"] + mqtt_publish.DISCOVERY_EVERY + 5)
            mqtt_publish.publish(s)
            assert any(t.startswith("homeassistant/") for t in _topics(clean, 4))
        finally:
            authed.delete(f"/api/printers/{extra['id']}")


def test_discovery_can_be_switched_off(authed, clean):
    _configure(authed, mqtt_discovery="false")
    with _session() as s:
        mqtt_publish.publish(s)
    assert not any(t.startswith("homeassistant/") for t in _topics(clean))


def test_a_broker_problem_is_remembered_and_never_raises_from_the_scheduler(authed, monkeypatch):
    def broken(messages, cfg):
        raise ConnectionRefusedError("broker is down")
    monkeypatch.setattr(mqtt_publish, "_send", broken)
    _configure(authed)
    with _session() as s:
        with pytest.raises(mqtt_publish.MqttError):
            mqtt_publish.publish(s)
        mqtt_publish.scheduled(s)                                              # logged, not raised
    assert "ConnectionRefusedError" in authed.get("/api/settings").json()["mqtt_last_error"]
    monkeypatch.setattr(mqtt_publish, "_send", lambda messages, cfg: None)
    with _session() as s:
        mqtt_publish.publish(s)
    assert authed.get("/api/settings").json()["mqtt_last_error"] == ""


def test_the_test_button_reports_success_and_failure(authed, clean, monkeypatch):
    assert authed.post("/api/settings/mqtt-test").status_code == 502                       # no broker set
    _configure(authed, mqtt_prefix="lab")
    r = authed.post("/api/settings/mqtt-test")
    assert r.status_code == 200 and "lab/test" in r.json()["message"]
    assert clean[-1][0][0][0] == "lab/test" and clean[-1][0][0][2] is False
    monkeypatch.setattr(mqtt_publish, "_send", lambda m, c: (_ for _ in ()).throw(OSError("no route")))
    bad = authed.post("/api/settings/mqtt-test")
    assert bad.status_code == 502 and "OSError" in bad.json()["detail"]


def test_the_password_is_masked_and_left_out_of_backups(authed):
    _configure(authed, mqtt_user="ha", mqtt_password="super-mqtt-secret")
    assert authed.get("/api/settings").json()["mqtt_password"] == "********"
    assert authed.get("/api/settings").json()["mqtt_user"] == "ha"
    data = authed.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert b"super-mqtt-secret" not in z.read("modelhub.db")
    authed.put("/api/settings", json={"mqtt_password": "********"})                          # an unchanged mask keeps the real one
    with _session() as s:
        assert mqtt_publish.settings(s)["auth"]["password"] == "super-mqtt-secret"


def test_the_real_sender_hands_paho_the_right_connection_details(authed, monkeypatch):
    import paho.mqtt.publish as publish
    calls = []
    monkeypatch.setattr(publish, "multiple", lambda msgs, **kw: calls.append((msgs, kw)))
    monkeypatch.undo()                                                                    # use the real _send (the fixture patched it)
    monkeypatch.setattr(publish, "multiple", lambda msgs, **kw: calls.append((msgs, kw)))
    cfg = {"host": "b.local", "port": 8883, "prefix": "m", "auth": {"username": "u", "password": "p"}, "tls": True, "discovery": True}
    mqtt_publish._send([("m/x", "1", True)], cfg)
    msgs, kw = calls[0]
    assert msgs == [{"topic": "m/x", "payload": "1", "retain": True, "qos": 0}]
    assert kw["hostname"] == "b.local" and kw["port"] == 8883 and kw["auth"] == {"username": "u", "password": "p"} and kw["tls"] == {}
    mqtt_publish._send([("m/y", "2", False)], {**cfg, "tls": False, "auth": None})
    assert calls[1][1]["tls"] is None and calls[1][1]["auth"] is None


def test_the_scheduler_has_the_mqtt_job_and_only_the_admin_sets_it_up(authed):
    assert "mqtt" in scheduler.INTERVALS and "mqtt" in scheduler._jobs()
    authed.post("/api/users", json={"username": "mqttmember", "password": PW})
    from app.main import app
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "mqttmember", "password": PW})
        assert member.post("/api/settings/mqtt-test").status_code == 403
        assert member.put("/api/settings", json={"mqtt_host": "evil"}).status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
