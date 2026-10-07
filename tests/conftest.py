import os
import tempfile

import pytest

# Must be set before app.config (and anything importing it) is ever imported --
# config.py reads these at import time to compute LIBRARY_PATH/CONFIG_PATH/DB_PATH.
_tmp = tempfile.mkdtemp(prefix="modelhub-test-")
os.environ["LIBRARY_PATH"] = os.path.join(_tmp, "data")
os.environ["CONFIG_PATH"] = os.path.join(_tmp, "config")
os.makedirs(os.environ["LIBRARY_PATH"], exist_ok=True)
os.makedirs(os.environ["CONFIG_PATH"], exist_ok=True)
# effectively disable the background scan loop firing mid-suite
os.environ["SCAN_INTERVAL_SECONDS"] = "999999"


@pytest.fixture(scope="session")
def library_path():
    return os.environ["LIBRARY_PATH"]


@pytest.fixture(scope="session")
def client():
    from starlette.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def authed(client):
    """The shared client, logged in. Other test files leave it logged out or
    never logged in, so set up / log in whichever way is needed."""
    if client.get("/api/settings").status_code == 200:
        return client
    if not client.get("/api/auth/status").json().get("configured"):
        creds = {"username": "fixture-admin", "password": "fixture admin password"}
        assert client.post("/api/auth/setup", json=creds).status_code == 200
        assert client.post("/api/auth/login", json=creds).status_code == 200
    else:
        # test_app.py ends logged out, with the password set in its change-password test
        login = client.post("/api/auth/login", json={"username": "admin", "password": "new password long enough"})
        assert login.status_code == 200
    return client
