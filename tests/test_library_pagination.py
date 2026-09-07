from datetime import datetime


def _ensure_authenticated(client):
    status = client.get("/api/auth/status").json()
    username = "pagination-test-admin"
    password = "pagination test password"
    if not status.get("configured"):
        setup = client.post("/api/auth/setup", json={"username": username, "password": password})
        assert setup.status_code == 200
        login = client.post("/api/auth/login", json={"username": username, "password": password})
        assert login.status_code == 200
        return

    # The shared integration client may already have a valid session from
    # tests/test_app.py. If so, no credentials need to be known here.
    if client.get("/api/settings").status_code == 200:
        return

    raise AssertionError("Test client is configured but not authenticated")


def _seed_pagination_models():
    from sqlmodel import Session
    from app.db import engine
    from app.models import Model3D

    with Session(engine) as session:
        for i in range(230):
            suffix = f"{i:03d}"
            path = f"pagination-fixture/model-{suffix}.stl"
            existing = session.exec(
                __import__("sqlmodel").select(Model3D).where(Model3D.path == path)
            ).first()
            if existing:
                continue
            session.add(Model3D(
                filename=f"pagination_model_{suffix}.stl",
                path=path,
                extension=".stl",
                size_bytes=1,
                content_hash=f"pagination-hash-{suffix}",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_scanned_at=datetime.utcnow(),
            ))
        session.commit()


def test_library_pagination_reports_total_and_returns_requested_page(client):
    _ensure_authenticated(client)
    _seed_pagination_models()

    response = client.get(
        "/api/library/models",
        params={"q": "pagination_model_", "limit": 50, "offset": 100},
    )

    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "230"
    assert response.headers["X-Limit"] == "50"
    assert response.headers["X-Offset"] == "100"
    items = response.json()
    assert len(items) == 50
    assert items[0]["filename"] == "pagination_model_100.stl"
    assert items[-1]["filename"] == "pagination_model_149.stl"


def test_search_applies_before_limit(client):
    _ensure_authenticated(client)
    _seed_pagination_models()

    # This record is well beyond the old first-200 result window. The old
    # implementation fetched 200 rows first and then searched in Python, so
    # this query could incorrectly return no result.
    response = client.get(
        "/api/library/models",
        params={"q": "pagination_model_229.stl"},
    )

    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "1"
    items = response.json()
    assert len(items) == 1
    assert items[0]["filename"] == "pagination_model_229.stl"
