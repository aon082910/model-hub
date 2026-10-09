"""Swapping spools with a Spoolman server, which is faked."""
import httpx
import pytest
from starlette.testclient import TestClient

from app import spoolman

PW = "a long enough password"
SPOOLS = [
    {"id": 7, "remaining_weight": 640.5, "initial_weight": 1000, "price": 22.0,
     "filament": {"name": "Galaxy Black", "material": "PETG", "color_hex": "1A1A1A", "weight": 1000, "vendor": {"name": "Prusament"}}},
    {"id": 8, "remaining_weight": 250, "filament": {"name": "Red", "material": "PLA", "color_hex": "FF0000", "weight": 750, "price": 18.0}},
    "junk", {"id": "x"},
]


class FakeSpoolman:
    def __init__(self):
        self.requests, self.status, self.vendors, self.next_id = [], 200, [{"id": 1, "name": "Prusament"}], 100

    def __call__(self, request):
        self.requests.append((request.method, request.url.path, request.read().decode() if request.method != "GET" else ""))
        if self.status != 200:
            return httpx.Response(self.status)
        path, method = request.url.path, request.method
        if method == "GET" and path == "/api/v1/info":
            return httpx.Response(200, json={"version": "0.22.1"})
        if method == "GET" and path == "/api/v1/spool":
            return httpx.Response(200, json=SPOOLS)
        if method == "GET" and path == "/api/v1/vendor":
            return httpx.Response(200, json=self.vendors)
        if method == "POST" and path in ("/api/v1/vendor", "/api/v1/filament", "/api/v1/spool"):
            self.next_id += 1
            return httpx.Response(200, json={"id": self.next_id})
        if method == "PUT" and path.startswith("/api/v1/spool/") and path.endswith("/use"):
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404)


@pytest.fixture()
def server(monkeypatch, authed):
    fake = FakeSpoolman()
    monkeypatch.setattr(spoolman, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    authed.put("/api/settings", json={"spoolman_url": "http://spoolman.local:7912", "spoolman_sync_usage": ""})
    yield fake
    authed.put("/api/settings", json={"spoolman_url": "", "spoolman_sync_usage": ""})
    for f in authed.get("/api/filament").json():
        authed.delete(f"/api/filament/{f['id']}")


def _spools(c):
    return {f["external_id"] or f["color"]: f for f in c.get("/api/filament").json()}


def test_the_connection_is_tested(authed, server):
    assert authed.post("/api/spoolman/test").json()["message"] == "Connected to Spoolman 0.22.1"
    server.status = 500
    assert authed.post("/api/spoolman/test").status_code == 502


@pytest.mark.parametrize("url", ["", "ftp://x", "http://user:pw@host:7912", "spoolman.local", "http://host:7912?x=1"])
def test_a_bad_address_is_refused_before_anything_is_sent(authed, server, url):
    authed.put("/api/settings", json={"spoolman_url": url})
    r = authed.post("/api/spoolman/test")
    assert r.status_code == 502 and server.requests == []


def test_import_brings_spools_in_once_and_refreshes_their_weight(authed, server):
    r = authed.post("/api/spoolman/import").json()
    assert r == {"added": 2, "updated": 0, "seen": 4}
    spools = _spools(authed)
    petg = spools["spoolman:7"]
    assert (petg["material"], petg["brand"], petg["color"], petg["color_hex"], petg["remaining_g"], petg["spool_weight_g"], petg["cost"]) == \
        ("PETG", "Prusament", "Galaxy Black", "#1a1a1a", 640.5, 1000, 22.0)
    red = spools["spoolman:8"]
    assert red["spool_weight_g"] == 750 and red["cost"] == 18.0 and red["brand"] is None                      # the filament's price when the spool has none
    SPOOLS[0]["remaining_weight"] = 600
    try:
        assert authed.post("/api/spoolman/import").json() == {"added": 0, "updated": 1, "seen": 4}
        assert _spools(authed)["spoolman:7"]["remaining_g"] == 600
    finally:
        SPOOLS[0]["remaining_weight"] = 640.5
    assert len([f for f in authed.get("/api/filament").json() if f["external_id"]]) == 2


def test_export_creates_the_vendor_filament_and_spool_once_per_spool(authed, server):
    authed.post("/api/filament", json={"material": "ABS", "brand": "Prusament", "color": "grey", "color_hex": "#808080", "spool_weight_g": 1000, "remaining_g": 700, "cost": 25})
    authed.post("/api/filament", json={"material": "TPU", "brand": "NewBrand", "color": "clear", "spool_weight_g": 500, "remaining_g": 500})
    assert authed.post("/api/spoolman/export").json() == {"created": 2}
    posts = [(m, p) for m, p, _ in server.requests if m == "POST"]
    assert posts.count(("POST", "/api/v1/vendor")) == 1                                                    # Prusament already existed; NewBrand did not
    assert posts.count(("POST", "/api/v1/filament")) == 2 and posts.count(("POST", "/api/v1/spool")) == 2
    filament_body = next(b for m, p, b in server.requests if p == "/api/v1/filament" and "ABS" in b)
    assert '"vendor_id":1' in filament_body.replace(" ", "") and '"color_hex":"808080"' in filament_body.replace(" ", "") and '"price":25' in filament_body.replace(" ", "")
    assert all(f["external_id"].startswith("spoolman:") for f in authed.get("/api/filament").json())
    assert authed.post("/api/spoolman/export").json() == {"created": 0}


def test_a_failure_part_way_keeps_what_was_done(authed, server, monkeypatch):
    authed.post("/api/filament", json={"material": "PLA", "color": "one", "spool_weight_g": 1000, "remaining_g": 900})
    authed.post("/api/filament", json={"material": "PLA", "color": "two", "spool_weight_g": 1000, "remaining_g": 900})
    real = server.__call__
    calls = {"n": 0}

    def flaky(request):
        if request.method == "POST" and request.url.path == "/api/v1/spool":
            calls["n"] += 1
            if calls["n"] == 2:
                return httpx.Response(500)
        return real(request)
    monkeypatch.setattr(spoolman, "_client", lambda: httpx.Client(transport=httpx.MockTransport(flaky)))
    assert authed.post("/api/spoolman/export").status_code == 502
    done = [f for f in authed.get("/api/filament").json() if f["external_id"]]
    assert len(done) == 1


def test_usage_is_reported_only_when_switched_on_and_never_stops_a_print(authed, server, monkeypatch):
    import uuid
    authed.post("/api/spoolman/import")
    spool = _spools(authed)["spoolman:7"]
    n = 30 + uuid.uuid4().int % 250
    stl = (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n endloop\nendfacet\nendsolid t\n").encode()
    m = authed.post("/api/library/import", files={"file": (f"sm_{uuid.uuid4().hex[:6]}.stl", stl, "application/octet-stream")}).json()
    try:
        def finish():
            item = authed.post("/api/queue", json={"model_id": m["id"], "filament_id": spool["id"], "estimated_grams": 20}).json()
            authed.patch(f"/api/queue/{item['id']}", json={"status": "done"})
        finish()
        assert not [r for r in server.requests if r[0] == "PUT"]                                          # off by default
        authed.put("/api/settings", json={"spoolman_sync_usage": "true"})
        finish()
        puts = [r for r in server.requests if r[0] == "PUT"]
        assert puts[0][1] == "/api/v1/spool/7/use" and '"use_weight":20' in puts[0][2].replace(" ", "")
        server.status = 500
        finish()                                                                                          # Spoolman down: the print is still recorded
        assert len(authed.get("/api/prints", params={"model_id": m["id"]}).json()["items"]) == 3
    finally:
        authed.delete(f"/api/library/models/{m['id']}")
        for q in authed.get("/api/queue").json():
            authed.delete(f"/api/queue/{q['id']}")


def test_only_the_administrator_may_use_it(authed, server):
    from app.main import app
    authed.post("/api/users", json={"username": "spmember", "password": PW, "role": "member"})
    try:
        member = TestClient(app)
        member.post("/api/auth/login", json={"username": "spmember", "password": PW})
        for path in ("test", "import", "export"):
            assert member.post(f"/api/spoolman/{path}").status_code == 403
    finally:
        for u in authed.get("/api/users").json()["users"]:
            authed.delete(f"/api/users/{u['id']}")
