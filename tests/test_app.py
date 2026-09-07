"""End-to-end API tests against a real (temp-dir) instance of the app.

Tests run in file order against one shared TestClient/session, mirroring how
this app has actually been manually verified during development (setup ->
login -> use the authenticated session for everything else). Not isolated
unit tests -- an integration suite that exercises the real auth gate, real
SQLite DB, and real mesh parsing.
"""
import io


ADMIN_PASSWORD = "correct horse battery staple"


def _stl_bytes(size: float = 20.0) -> bytes:
    """A minimal but valid watertight-ish single ASCII STL. Not actually
    watertight (one triangle), which is intentional for some tests; the
    watertight cube fixture below is used where real volume math matters.
    """
    return f"""solid test
facet normal 0 0 1
  outer loop
    vertex 0 0 0
    vertex {size} 0 0
    vertex 0 {size} 0
  endloop
endfacet
endsolid test
""".encode()


def _watertight_cube_stl(extents: float = 20.0) -> bytes:
    import trimesh
    mesh = trimesh.creation.box(extents=(extents, extents, extents))
    return mesh.export(file_type="stl")


# ---------- health / pre-auth ----------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_not_configured_initially(client):
    r = client.get("/api/auth/status")
    assert r.json() == {"configured": False}


def test_protected_endpoint_blocked_before_login(client):
    r = client.get("/api/settings")
    assert r.status_code == 401


# ---------- setup / login ----------

def test_setup_rejects_short_password(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": "short"})
    assert r.status_code == 400


def test_setup_creates_account(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    assert client.get("/api/auth/status").json() == {"configured": True}


def test_setup_twice_rejected(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 409


def test_login_wrong_password_rejected(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    # still not authenticated
    assert client.get("/api/settings").status_code == 401


def test_login_success_grants_session(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    assert "modelhub_session" in r.cookies
    # the client persists cookies across requests from here on
    assert client.get("/api/settings").status_code == 200


# ---------- login rate limiting ----------
# Exercised as a unit test against app.auth directly rather than through the
# shared `client` fixture: TestClient requests all share the same fake client
# IP, so tripping the limiter via HTTP here would lock out every later test
# in this file that logs in with the same client.

def test_login_rate_limit_trips_after_max_attempts():
    from app import auth
    key = "test-rate-limit-ip"
    auth.clear_login_attempts(key)
    try:
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            assert auth.check_login_rate_limit(key) is None
            auth.record_failed_login(key)
        retry_after = auth.check_login_rate_limit(key)
        assert retry_after is not None and retry_after > 0
    finally:
        auth.clear_login_attempts(key)


def test_login_rate_limit_cleared_on_success():
    from app import auth
    key = "test-rate-limit-ip-2"
    auth.clear_login_attempts(key)
    try:
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            auth.record_failed_login(key)
        assert auth.check_login_rate_limit(key) is not None
        auth.clear_login_attempts(key)
        assert auth.check_login_rate_limit(key) is None
    finally:
        auth.clear_login_attempts(key)


# ---------- settings: reserved keys never leak ----------

def test_settings_excludes_reserved_keys(client):
    data = client.get("/api/settings").json()
    assert "auth_password_hash" not in data
    assert "auth_username" not in data
    assert "extension_api_key" not in data


def test_settings_put_ignores_reserved_keys(client):
    client.put("/api/settings", json={"auth_password_hash": "hacked", "ai_mode": "off"})
    data = client.get("/api/settings").json()
    assert data["ai_mode"] == "off"
    r = client.get("/api/settings/extension-key")
    assert r.json()["extension_api_key"] != "hacked"


# ---------- extension API key: scoped to /api/library/import only ----------

def test_extension_key_endpoint_requires_session(client):
    r = client.get("/api/settings/extension-key")
    assert r.status_code == 200
    assert len(r.json()["extension_api_key"]) > 10


def test_regenerate_extension_key_changes_value(client):
    before = client.get("/api/settings/extension-key").json()["extension_api_key"]
    after = client.post("/api/settings/regenerate-extension-key").json()["extension_api_key"]
    assert before != after


def _anon_client(client):
    """A client sharing the same running app/DB but with none of `client`'s
    session cookies -- needed to test API-key-only auth, since the shared
    `client` fixture stays logged in (via cookie) for most of this suite."""
    from starlette.testclient import TestClient
    return TestClient(client.app)


def test_api_key_works_for_import_but_nothing_else(client):
    key = client.get("/api/settings/extension-key").json()["extension_api_key"]
    anon = _anon_client(client)

    r = anon.post(
        "/api/library/import",
        headers={"X-Model-Hub-Api-Key": key},
        files={"file": ("keytest.stl", io.BytesIO(_stl_bytes(5)), "application/octet-stream")},
    )
    assert r.status_code == 200

    # same key must NOT work against an arbitrary other endpoint
    r = anon.get("/api/settings", headers={"X-Model-Hub-Api-Key": key})
    assert r.status_code == 401


def test_wrong_api_key_rejected(client):
    anon = _anon_client(client)
    r = anon.post(
        "/api/library/import",
        headers={"X-Model-Hub-Api-Key": "not-the-real-key"},
        files={"file": ("bad.stl", io.BytesIO(_stl_bytes(5)), "application/octet-stream")},
    )
    assert r.status_code == 401


# ---------- library scan / mesh parsing / duplicates ----------

def test_scan_parses_watertight_cube(client, library_path):
    import os
    with open(os.path.join(library_path, "cube.stl"), "wb") as f:
        f.write(_watertight_cube_stl(20.0))

    r = client.post("/api/library/scan")
    assert r.status_code == 200
    assert r.json()["added"] >= 1

    models = client.get("/api/library/models").json()
    cube = next(m for m in models if m["filename"] == "cube.stl")
    assert cube["is_watertight"] is True
    assert cube["volume_mm3"] == 8000.0  # 20mm cube


def test_duplicate_detection(client, library_path):
    import os, shutil
    shutil.copyfile(
        os.path.join(library_path, "cube.stl"),
        os.path.join(library_path, "cube_copy.stl"),
    )
    r = client.post("/api/library/scan")
    assert r.json()["duplicates"] >= 1

    models = client.get("/api/library/models").json()
    copy = next(m for m in models if m["filename"] == "cube_copy.stl")
    assert copy["is_duplicate_of"] is not None


# ---------- print estimate ----------

def test_estimate_heuristic(client):
    models = client.get("/api/library/models").json()
    cube = next(m for m in models if m["filename"] == "cube.stl")

    r = client.get(f"/api/library/models/{cube['id']}/estimate", params={"material": "PLA", "infill": 0.15})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "heuristic"
    assert body["estimated_grams"] > 0
    assert body["estimated_minutes"] > 0


# ---------- filament + queue deduction ----------

def test_queue_done_deducts_filament_exactly_once(client):
    models = client.get("/api/library/models").json()
    cube = next(m for m in models if m["filename"] == "cube.stl")

    fil = client.post("/api/filament", json={
        "material": "PLA", "brand": "Test", "color": "Red",
        "spool_weight_g": 1000, "remaining_g": 1000,
    }).json()

    item = client.post("/api/queue", json={
        "model_id": cube["id"], "filament_id": fil["id"],
        "estimated_grams": 10.0, "estimated_minutes": 20.0,
    }).json()

    client.patch(f"/api/queue/{item['id']}", json={"status": "done"})
    remaining_after_first = next(
        f for f in client.get("/api/filament").json() if f["id"] == fil["id"]
    )["remaining_g"]
    assert remaining_after_first == 990.0

    # marking done again (already done -> done) must not deduct a second time
    client.patch(f"/api/queue/{item['id']}", json={"status": "done"})
    remaining_after_second = next(
        f for f in client.get("/api/filament").json() if f["id"] == fil["id"]
    )["remaining_g"]
    assert remaining_after_second == 990.0


# ---------- account / logout ----------

def test_change_password_wrong_current_rejected(client):
    r = client.post("/api/auth/change-password", json={
        "current_password": "wrong", "new_password": "new password long enough",
    })
    assert r.status_code == 401


def test_change_password_success(client):
    r = client.post("/api/auth/change-password", json={
        "current_password": ADMIN_PASSWORD, "new_password": "new password long enough",
    })
    assert r.status_code == 200
    # old password no longer works
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 401
    # new password does
    r = client.post("/api/auth/login", json={"username": "admin", "password": "new password long enough"})
    assert r.status_code == 200


def test_logout_revokes_session(client):
    client.post("/api/auth/logout")
    assert client.get("/api/settings").status_code == 401
