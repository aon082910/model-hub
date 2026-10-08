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


# Accounts other test files leave behind, most likely first. Wrong guesses count
# toward the login rate limit (5 failures), so the list stays short.
KNOWN_ACCOUNTS = [
    ("admin", "new password long enough"),                    # test_app.py (after its password change)
    ("fixture-admin", "fixture admin password"),              # created here when nothing else has
    ("pagination-test-admin", "pagination test password"),    # test_library_pagination.py
]


def ensure_authenticated(client):
    """Log the shared client in, however the earlier tests left it: logged in,
    logged out, or not set up at all. Works for any subset of the suite."""
    if client.get("/api/settings").status_code == 200:
        return client
    if not client.get("/api/auth/status").json().get("configured"):
        username, password = KNOWN_ACCOUNTS[1]
        assert client.post("/api/auth/setup", json={"username": username, "password": password}).status_code == 200
    for username, password in KNOWN_ACCOUNTS:
        if client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200:
            return client
    raise AssertionError("could not log in as any known test account")


@pytest.fixture()
def authed(client):
    return ensure_authenticated(client)
