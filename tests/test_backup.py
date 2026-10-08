"""Backing up and restoring the data: round trip, what a backup leaves out, and refusing bad files."""
import io
import json
import sqlite3
import zipfile

import pytest

from app import backup, downloads, sources
from app.config import DB_PATH


@pytest.fixture(autouse=True)
def clean(authed):
    yield
    for item in authed.get("/api/backup/saved").json()["backups"]:
        authed.delete(f"/api/backup/saved/{item['name']}")
    authed.put("/api/settings", json={"thingiverse_token": "", "ai_api_key": ""})
    downloads._items.clear()


def _zip_bytes(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return out.getvalue()


def _download(c) -> bytes:
    r = c.get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    return r.content


def _restore(c, data: bytes, confirm="replace"):
    return c.post("/api/backup/restore", files={"file": ("b.zip", data, "application/zip")}, data={"confirm": confirm})


def _tag_names(c):
    """Names of the collections (the simplest data to create and look for)."""
    return sorted(x["name"] for x in c.get("/api/collections").json())


def test_backup_holds_the_data_but_no_secrets(authed):
    authed.put("/api/settings", json={"thingiverse_token": "super-secret-token", "ai_api_key": "sk-secret", "viewer_model_color": "#112233"})
    data = _download(authed)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert set(z.namelist()) >= {"modelhub.db", "manifest.json"}
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["format"] == 1 and "model3d" in manifest["tables"]
        z.extract("modelhub.db", path=str(DB_PATH.parent / "peek"))
    conn = sqlite3.connect(DB_PATH.parent / "peek" / "modelhub.db")
    try:
        kept = dict(conn.execute("SELECT key, value FROM appsettings").fetchall())
    finally:
        conn.close()
    assert kept.get("viewer_model_color") == "#112233"
    for secret in ("thingiverse_token", "ai_api_key", "auth_password_hash", "extension_api_key", "auth_username"):
        assert secret not in kept
    assert b"super-secret-token" not in data and b"sk-secret" not in data


def test_round_trip_restores_data_and_keeps_current_logins(authed):
    authed.post("/api/collections", json={"name": "backup-keep-me"})
    wish = authed.post("/api/wishlist", json={"provider": "printables", "source_id": "4321", "title": "Backed up wish", "note": "n"}).json()
    data = _download(authed)

    authed.post("/api/collections", json={"name": "added-after-backup"})
    authed.delete(f"/api/wishlist/{wish['id']}")
    authed.put("/api/settings", json={"thingiverse_token": "token-set-after-backup"})
    assert "backup-keep-me" in _tag_names(authed) and "added-after-backup" in _tag_names(authed)

    r = _restore(authed, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["safety_copy"].startswith("before-restore-") and body["restored"]["collection"] >= 1
    names = _tag_names(authed)
    assert "backup-keep-me" in names and "added-after-backup" not in names
    assert [w["title"] for w in authed.get("/api/wishlist").json()] == ["Backed up wish"]
    assert authed.get("/api/settings").status_code == 200          # still logged in: auth settings were not replaced
    assert authed.get("/api/settings").json()["thingiverse_token"] == "********"   # the token set after the backup survives
    authed.delete(f"/api/wishlist/{wish['id']}")


def test_the_safety_copy_can_undo_a_restore(authed):
    authed.post("/api/collections", json={"name": "before-undo"})
    older = _download(authed)
    authed.post("/api/collections", json={"name": "newer-than-backup"})
    saved = _restore(authed, older).json()["safety_copy"]
    assert "newer-than-backup" not in _tag_names(authed)
    r = authed.post(f"/api/backup/saved/{saved}/restore", json={"confirm": "replace"})
    assert r.status_code == 200
    assert "newer-than-backup" in _tag_names(authed)


def test_pictures_are_backed_up_and_restored(authed):
    folder = sources.IMAGE_ROOT / "98765"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "1.jpg").write_bytes(b"jpgdata")
    data = _download(authed)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "source_images/98765/1.jpg" in z.namelist()
    (folder / "1.jpg").unlink()
    assert _restore(authed, data).json()["pictures"] >= 1
    assert (folder / "1.jpg").read_bytes() == b"jpgdata"
    (folder / "1.jpg").unlink()
    folder.rmdir()


def test_confirmation_is_required(authed):
    data = _download(authed)
    assert _restore(authed, data, confirm="").status_code == 400
    assert authed.post("/api/backup/saved/x.zip/restore", json={}).status_code == 400


def good_db_bytes():
    with zipfile.ZipFile(io.BytesIO(_download_cached[0])) as z:
        return z.read("modelhub.db")


_download_cached = [b""]


@pytest.fixture()
def good_backup(authed):
    _download_cached[0] = _download(authed)
    return _download_cached[0]


@pytest.mark.parametrize("make,fragment", [
    (lambda good: b"this is not a zip", "not a zip"),
    (lambda good: _zip_bytes({"notes.txt": "hi"}), "no modelhub.db"),
    (lambda good: _zip_bytes({"modelhub.db": b"not a database"}), "can't be read"),
    (lambda good: _zip_bytes({"modelhub.db": good_db_bytes(), "../escape.txt": "x"}), "should not"),
    (lambda good: _zip_bytes({"modelhub.db": good_db_bytes(), "source_images/../../escape.txt": "x"}), "should not"),
    (lambda good: _zip_bytes({"modelhub.db": good_db_bytes(), "something/else.bin": "x"}), "should not"),
    (lambda good: _zip_bytes({"modelhub.db": good_db_bytes(), "manifest.json": json.dumps({"format": 99})}), "newer version"),
])
def test_bad_backups_are_refused_and_change_nothing(authed, good_backup, make, fragment):
    authed.post("/api/collections", json={"name": "must-survive-a-bad-restore"})
    r = _restore(authed, make(good_backup))
    assert r.status_code == 400 and fragment in r.json()["detail"], r.text
    assert "must-survive-a-bad-restore" in _tag_names(authed)


def test_a_database_without_the_model_table_is_refused(authed, tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE unrelated (a INTEGER)")
    conn.commit()
    conn.close()
    r = _restore(authed, _zip_bytes({"modelhub.db": other.read_bytes()}))
    assert r.status_code == 400 and "not a Model Hub database" in r.json()["detail"]


def test_an_older_backup_without_newer_columns_still_restores(authed, tmp_path):
    data = _download(authed)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        db_bytes = z.read("modelhub.db")
    old = tmp_path / "old.db"
    old.write_bytes(db_bytes)
    conn = sqlite3.connect(old)
    conn.execute("DROP TABLE wishlistitem")                     # a backup from before the wishlist existed
    conn.execute("ALTER TABLE model3d DROP COLUMN notes")        # ...and before notes
    conn.commit()
    conn.close()
    r = _restore(authed, _zip_bytes({"modelhub.db": old.read_bytes()}))
    assert r.status_code == 200, r.text
    assert authed.get("/api/wishlist").json() == []
    assert authed.get("/api/library/models").status_code == 200


def test_restore_waits_for_running_downloads(authed):
    data = _download(authed)
    downloads._items.append({"id": "x", "provider": "printables", "source_id": "1", "status": "downloading"})
    r = _restore(authed, data)
    assert r.status_code == 400 and "Downloads are still running" in r.json()["detail"]


def test_saved_copies_list_download_and_delete(authed):
    saved = authed.post("/api/backup/save").json()
    assert saved["name"].startswith("modelhub-backup-") and saved["size"] > 0
    listed = authed.get("/api/backup/saved").json()["backups"]
    assert [b["name"] for b in listed] == [saved["name"]]
    got = authed.get(f"/api/backup/saved/{saved['name']}")
    assert got.status_code == 200 and zipfile.is_zipfile(io.BytesIO(got.content))
    assert authed.delete(f"/api/backup/saved/{saved['name']}").status_code == 200
    assert authed.get(f"/api/backup/saved/{saved['name']}").status_code == 404


@pytest.mark.parametrize("name", ["../modelhub.db", "..%2Fmodelhub.db", "a/b.zip", "notazip.txt", ".hidden.zip"])
def test_saved_names_cannot_escape_the_folder(authed, name):
    assert authed.get(f"/api/backup/saved/{name}").status_code in (404, 422)
    assert authed.delete(f"/api/backup/saved/{name}").status_code in (404, 405, 422)
    assert authed.post(f"/api/backup/saved/{name}/restore", json={"confirm": "replace"}).status_code in (404, 405, 422)


def test_only_the_newest_safety_copies_are_kept(authed, monkeypatch):
    stamps = iter(f"20200101-00000{i}" for i in range(6))
    monkeypatch.setattr(backup, "_stamp", lambda: next(stamps))
    for _ in range(5):
        backup.save_backup("before-restore")
    kept = [b["name"] for b in backup.saved_backups() if b["name"].startswith("before-restore-")]
    assert len(kept) == backup.KEEP_SAFETY_COPIES


def test_backup_needs_a_login(authed):
    authed.post("/api/auth/logout")
    try:
        assert authed.get("/api/backup").status_code == 401
        assert authed.post("/api/backup/save").status_code == 401
        assert authed.post("/api/backup/restore", files={"file": ("b.zip", b"x")}, data={"confirm": "replace"}).status_code == 401
    finally:
        from conftest import ensure_authenticated
        ensure_authenticated(authed)


def test_two_saved_copies_in_the_same_second_never_replace_each_other(authed, monkeypatch):
    monkeypatch.setattr(backup, "_stamp", lambda: "20300101-000000")
    a = backup.save_backup("modelhub-backup")
    b = backup.save_backup("modelhub-backup")
    c = backup.save_backup("modelhub-backup")
    assert len({a["name"], b["name"], c["name"]}) == 3
    names = {x["name"] for x in backup.saved_backups()}
    assert {a["name"], b["name"], c["name"]} <= names
    for n in (a["name"], b["name"], c["name"]):
        assert backup.saved_path(n).is_file()
