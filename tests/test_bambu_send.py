"""Sending a sliced .gcode.3mf to a Bambu printer in LAN mode and starting it.
The printer is never contacted: the FTP upload, the MQTT start and the state report are replaced by fakes, except for one test
that runs the real upload code against a small in-process implicit-FTPS server."""
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

from app import bambu_send, print_files, printers as printing, printwatch
from app.printers import PrinterError

SERIAL = "01P00A123456789"
CODE = "12345678"


def sliced_3mf(plates=(1,), extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", "<model/>")
        for n in plates:
            z.writestr(f"Metadata/plate_{n}.gcode", "G28\n")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def plain_3mf() -> bytes:
    return sliced_3mf(plates=())


# ---------- unit tests ----------

def test_plates_are_found_in_a_sliced_3mf_and_only_there(tmp_path):
    f = tmp_path / "a.gcode.3mf"
    f.write_bytes(sliced_3mf(plates=(2, 1, 10)))
    assert bambu_send.plate_numbers(f) == [1, 2, 10]
    g = tmp_path / "b.3mf"
    g.write_bytes(plain_3mf())
    assert bambu_send.plate_numbers(g) == []
    h = tmp_path / "c.3mf"
    h.write_bytes(b"not a zip")
    assert bambu_send.plate_numbers(h) == []
    assert bambu_send.plate_numbers(tmp_path / "missing.3mf") == []


@pytest.mark.parametrize("name,expected", [
    ("benchy.gcode.3mf", "benchy.gcode.3mf"), ("My Model (v2).3mf", "My_Model_v2.gcode.3mf"), ("../../etc/x.3mf", "x.gcode.3mf"), (r"C:\Users\me\part.3mf", "part.gcode.3mf"),
    ("BENCHY.GCODE.3MF", "BENCHY.gcode.3mf"), ("日本語.3mf", "model.gcode.3mf"), ("a" * 200 + ".3mf", "a" * 80 + ".gcode.3mf")])
def test_the_name_put_on_the_printers_card_is_safe(name, expected):
    assert bambu_send.job_name(name) == expected


def test_the_ams_mapping_is_checked():
    assert bambu_send.parse_mapping("") == [] and bambu_send.parse_mapping(None) == []
    assert bambu_send.parse_mapping("0, 1,-1,254,15") == [0, 1, -1, 254, 15]
    assert bambu_send.parse_mapping([2, "3"]) == [2, 3]
    for bad in ("a", "16", "-2", "1.5", "253"):
        with pytest.raises(PrinterError):
            bambu_send.parse_mapping(bad)


def test_the_start_message_has_the_shape_the_printer_expects():
    p = bambu_send.start_payload("benchy.gcode.3mf", 2, True, [0, 1])["print"]
    assert p["command"] == "project_file" and p["param"] == "Metadata/plate_2.gcode"
    assert p["file"] == "benchy.gcode.3mf" and p["url"] == "ftp:///benchy.gcode.3mf"
    assert p["use_ams"] is True and p["ams_mapping"] == [0, 1] and p["bed_type"] == "textured_plate" and p["skip_objects"] is None
    assert bambu_send.start_payload("x.gcode.3mf", 1, False, [])["print"]["use_ams"] is False


# ---------- through the API, with a fake printer ----------

@pytest.fixture()
def printer(monkeypatch, authed):
    state = {"state": "IDLE", "uploads": [], "starts": [], "upload_error": None, "start_error": None, "starts_when_told": True}

    def report(host, serial, code, timeout=printing.BAMBU_TIMEOUT):
        return {"gcode_state": state["state"]}

    def upload(host, code, path, remote_name, **kw):
        if state["upload_error"]:
            raise PrinterError(state["upload_error"])
        state["uploads"].append((host, code, remote_name, Path(path).read_bytes()))

    def start(host, serial, code, payload, **kw):
        if state["start_error"]:
            raise PrinterError(state["start_error"])
        state["starts"].append((host, serial, code, payload))
        if state["starts_when_told"]:
            state["state"] = "RUNNING"

    monkeypatch.setattr(printing, "_bambu_report", report)
    monkeypatch.setattr(bambu_send, "_ftp_upload", upload)
    monkeypatch.setattr(bambu_send, "_mqtt_start", start)
    monkeypatch.setattr(bambu_send.time, "sleep", lambda s: None)
    monkeypatch.setattr(bambu_send, "START_WAIT_SECONDS", 0.0)
    printwatch.reset()
    body = {"name": "P1S", "kind": "bambu", "url": "192.168.1.60", "serial": SERIAL, "api_key": CODE}
    r = authed.post("/api/printers", json=body)
    assert r.status_code == 200, r.text
    state["id"] = r.json()["id"]
    yield state
    for p in authed.get("/api/printers").json()["printers"]:
        authed.delete(f"/api/printers/{p['id']}")


def _send(c, state, data=None, name="benchy.gcode.3mf", **form):
    files = {"file": (name, data if data is not None else sliced_3mf(), "application/octet-stream")}
    return c.post(f"/api/printers/{state['id']}/send-3mf", files=files, data={k: str(v) for k, v in form.items()})


def test_a_file_can_be_put_on_the_card_without_starting_it(authed, printer):
    r = _send(authed, printer, start="false")
    assert r.status_code == 200, r.text
    assert r.json() == {"filename": "benchy.gcode.3mf", "started": False, "plate": 1}
    assert [u[:3] for u in printer["uploads"]] == [("192.168.1.60", CODE, "benchy.gcode.3mf")]
    assert printer["starts"] == []


def test_starting_sends_the_plate_and_slot_and_reports_that_the_printer_began(authed, printer):
    r = _send(authed, printer, data=sliced_3mf(plates=(1, 2)), start="true", plate=2, ams_mapping="3")
    assert r.status_code == 200, r.text
    assert r.json() == {"filename": "benchy.gcode.3mf", "started": True, "plate": 2}
    (host, serial, code, payload), = printer["starts"]
    assert (host, serial, code) == ("192.168.1.60", SERIAL, CODE)
    assert payload["print"]["param"] == "Metadata/plate_2.gcode" and payload["print"]["ams_mapping"] == [3]
    assert printer["uploads"][0][2] == payload["print"]["file"]


def test_the_external_spool_needs_no_mapping(authed, printer):
    r = _send(authed, printer, start="true", use_ams="false")
    assert r.status_code == 200, r.text
    assert printer["starts"][0][3]["print"]["use_ams"] is False


def test_a_start_with_the_ams_on_needs_slots_chosen_and_uploads_nothing(authed, printer):
    r = _send(authed, printer, start="true", use_ams="true")
    assert r.status_code == 502 and "AMS slot" in r.json()["detail"]
    assert printer["uploads"] == [] and printer["starts"] == []


def test_a_printer_that_ignores_the_start_is_reported_not_trusted(authed, printer):
    printer["starts_when_told"] = False
    r = _send(authed, printer, start="true", use_ams="false")
    assert r.status_code == 502 and "did not start" in r.json()["detail"] and "Developer Mode" in r.json()["detail"]
    assert len(printer["uploads"]) == 1            # the file did reach the card


@pytest.mark.parametrize("busy", ["RUNNING", "PREPARE", "PAUSE"])
def test_a_busy_printer_is_left_alone(authed, printer, busy):
    printer["state"] = busy
    r = _send(authed, printer, start="true", use_ams="false")
    assert r.status_code == 502 and "busy" in r.json()["detail"]
    assert printer["uploads"] == [] and printer["starts"] == []


def test_files_that_are_not_sliced_are_refused_with_the_reason(authed, printer):
    r = _send(authed, printer, data=plain_3mf(), name="part.3mf", start="false")
    assert r.status_code == 502 and "no sliced G-code" in r.json()["detail"]
    r = _send(authed, printer, data=b"G28", name="part.gcode", start="false")
    assert r.status_code == 400
    r = _send(authed, printer, data=sliced_3mf(), start="false", plate=3)
    assert r.status_code == 502 and "Plate 3" in r.json()["detail"]
    r = _send(authed, printer, start="false", bed_type="glass")
    assert r.status_code == 502 and "bed type" in r.json()["detail"]
    assert printer["uploads"] == []


def test_a_request_names_one_source_and_only_bambu_printers_take_it(authed, printer):
    r = authed.post(f"/api/printers/{printer['id']}/send-3mf", data={"start": "false"})
    assert r.status_code == 400
    r = authed.post(f"/api/printers/{printer['id']}/send-3mf", data={"print_file_id": "1"},
                    files={"file": ("a.3mf", sliced_3mf(), "application/octet-stream")})
    assert r.status_code == 400
    other = authed.post("/api/printers", json={"name": "Klipper", "kind": "moonraker", "url": "http://10.0.0.5:7125"}).json()
    r = authed.post(f"/api/printers/{other['id']}/send-3mf", files={"file": ("a.3mf", sliced_3mf(), "application/octet-stream")})
    assert r.status_code == 400 and "Bambu" in r.json()["detail"]


def test_errors_never_contain_the_access_code(authed, printer):
    printer["upload_error"] = "The printer refused the access code"
    r = _send(authed, printer, start="false")
    assert r.status_code == 502 and CODE not in r.text
    r = authed.get(f"/api/printers/{printer['id']}")
    assert CODE not in r.text


def test_a_kept_sliced_file_can_be_sent_and_the_job_is_remembered(authed, printer):
    from sqlmodel import Session, select
    from app.db import engine
    from app.models import PrintFile, PrinterJob
    stored = "test-bambu-send.gcode.3mf"
    path = print_files.stored_path(stored)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sliced_3mf())
    with Session(engine) as s:
        kept = PrintFile(model_id=4242, filename="Cool Part.gcode.3mf", stored_name=stored, kind="3mf")
        gcode = PrintFile(model_id=4242, filename="x.gcode", stored_name="x.gcode", kind="gcode")
        s.add(kept)
        s.add(gcode)
        s.commit()
        kept_id, gcode_id = kept.id, gcode.id
    try:
        r = authed.post(f"/api/printers/{printer['id']}/send-3mf", data={"print_file_id": str(kept_id), "start": "true", "use_ams": "false"})
        assert r.status_code == 200, r.text
        assert printer["uploads"][0][2] == "Cool_Part.gcode.3mf"
        with Session(engine) as s:
            job = s.exec(select(PrinterJob).where(PrinterJob.print_file_id == kept_id)).first()
            assert job and job.model_id == 4242 and job.started is True and job.filename == "Cool_Part.gcode.3mf"
        r = authed.post(f"/api/printers/{printer['id']}/send-3mf", data={"print_file_id": str(gcode_id)})
        assert r.status_code == 400 and "plain G-code" in r.json()["detail"]
        assert authed.post(f"/api/printers/{printer['id']}/send-3mf", data={"print_file_id": "999999"}).status_code == 404
    finally:
        path.unlink(missing_ok=True)
        with Session(engine) as s:
            for pf in s.exec(select(PrintFile).where(PrintFile.model_id == 4242)).all():
                s.delete(pf)
            s.commit()


# ---------- the real upload code against an in-process implicit-FTPS server ----------

class FakeBambuFTPS:
    """Implicit FTPS like the printer: TLS from the first byte, USER/PASS, PBSZ/PROT, PASV, STOR. Stores what it is sent."""

    def __init__(self, cert: str, key: str, code: str):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.code = code
        self.stored = {}
        self.log = []
        self.reject_stor = False
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(2)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        try:
            self.listener.close()
        except OSError:
            pass

    def _serve(self):
        try:
            raw, _ = self.listener.accept()
            conn = self.ctx.wrap_socket(raw, server_side=True)
            self._session(conn)
        except (OSError, ssl.SSLError):
            pass

    def _session(self, conn):
        f = conn.makefile("rwb", buffering=0)
        def say(text):
            f.write((text + "\r\n").encode())
        say("220 Bambu FTP ready")
        user_ok = prot_p = False
        data_listener = None
        reader = conn.makefile("rb")
        while line := reader.readline():
            cmd, _, arg = line.decode().strip().partition(" ")
            cmd = cmd.upper()
            self.log.append(cmd)
            if cmd == "USER":
                user_ok = arg == "bblp"
                say("331 password please")
            elif cmd == "PASS":
                say("230 ok" if user_ok and arg == self.code else "530 login incorrect")
            elif cmd == "PBSZ":
                say("200 PBSZ=0")
            elif cmd == "PROT":
                prot_p = arg.upper() == "P"
                say("200 protection set")
            elif cmd == "TYPE":
                say("200 binary")
            elif cmd == "PASV":
                data_listener = socket.socket()
                data_listener.bind(("127.0.0.1", 0))
                data_listener.listen(1)
                p = data_listener.getsockname()[1]
                say(f"227 Entering Passive Mode (127,0,0,1,{p >> 8},{p & 255})")
            elif cmd == "STOR":
                if self.reject_stor:
                    say("550 cannot write")
                    continue
                dconn, _ = data_listener.accept()
                say("150 opening data connection")
                assert prot_p, "data channel must be protected"
                tls = self.ctx.wrap_socket(dconn, server_side=True)
                chunks = []
                try:
                    while chunk := tls.recv(65536):
                        chunks.append(chunk)
                except (ssl.SSLError, OSError):
                    pass
                self.stored[arg] = b"".join(chunks)
                try:
                    tls.close()
                except OSError:
                    pass
                say("226 Transfer complete")
            elif cmd == "QUIT":
                say("221 bye")
                break
            else:
                say("502 not implemented")


@pytest.fixture()
def ftps(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed to make a throwaway certificate for the fake printer")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "2", "-subj", "/CN=printer"], check=True, capture_output=True)
    server = FakeBambuFTPS(str(cert), str(key), CODE)
    yield server
    server.close()


def test_the_real_upload_code_talks_implicit_ftps_and_the_file_arrives_whole(ftps, tmp_path):
    data = sliced_3mf(extra={"Metadata/big.bin": os.urandom(300_000)})
    src = tmp_path / "job.gcode.3mf"
    src.write_bytes(data)
    bambu_send._ftp_upload("127.0.0.1", CODE, src, "job.gcode.3mf", port=ftps.port, timeout=10)
    assert ftps.stored["job.gcode.3mf"] == data
    assert {"USER", "PASS", "PBSZ", "PROT", "PASV", "STOR"} <= set(ftps.log)


def test_a_wrong_access_code_and_a_refused_write_become_plain_messages(ftps, tmp_path):
    src = tmp_path / "job.gcode.3mf"
    src.write_bytes(sliced_3mf())
    with pytest.raises(PrinterError, match="access code"):
        bambu_send._ftp_upload("127.0.0.1", "wrong", src, "job.gcode.3mf", port=ftps.port, timeout=10)


def test_a_refused_write_mentions_the_card(tmp_path, ftps):
    ftps.reject_stor = True
    src = tmp_path / "job.gcode.3mf"
    src.write_bytes(sliced_3mf())
    with pytest.raises(PrinterError, match="microSD"):
        bambu_send._ftp_upload("127.0.0.1", CODE, src, "job.gcode.3mf", port=ftps.port, timeout=10)


def test_an_unreachable_printer_is_a_plain_message(tmp_path):
    src = tmp_path / "job.gcode.3mf"
    src.write_bytes(sliced_3mf())
    with pytest.raises(PrinterError, match="Could not upload"):
        bambu_send._ftp_upload("127.0.0.1", CODE, src, "job.gcode.3mf", port=1, timeout=2)


# ---------- the MQTT start, with paho replaced ----------

def test_the_start_message_is_published_to_the_printers_request_topic(monkeypatch):
    seen = {}

    class FakeInfo:
        def wait_for_publish(self, timeout=None): pass
        def is_published(self): return True

    class FakeClient:
        def __init__(self, *a, **k): seen["client_id"] = k.get("client_id")
        def username_pw_set(self, u, p): seen["auth"] = (u, p)
        def tls_set_context(self, ctx): seen["tls"] = ctx.verify_mode == ssl.CERT_NONE
        def connect(self, host, port, keepalive=60): seen["addr"] = (host, port)
        def loop_start(self): pass
        def loop_stop(self): pass
        def disconnect(self): pass
        def publish(self, topic, payload, qos=0):
            seen["topic"], seen["payload"], seen["qos"] = topic, json.loads(payload), qos
            return FakeInfo()

    import paho.mqtt.client as mqtt
    monkeypatch.setattr(mqtt, "Client", FakeClient)
    payload = bambu_send.start_payload("a.gcode.3mf", 1, False, [])
    bambu_send._mqtt_start("192.168.1.60", SERIAL, CODE, payload)
    assert seen["addr"] == ("192.168.1.60", 8883) and seen["auth"] == ("bblp", CODE)
    assert seen["topic"] == f"device/{SERIAL}/request" and seen["payload"] == payload and seen["qos"] == 1
