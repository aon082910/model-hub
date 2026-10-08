"""Noticing that a linked model's online listing changed."""
import copy
import time

import httpx
import pytest

from app import source_updates as su
from app import sources
from test_sources import FakeSites, PRINTABLES_FILES, PRINTABLES_PRINT


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


@pytest.fixture()
def sites(monkeypatch, authed):
    fake = FakeSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(su, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(su, "BACKOFF_SECONDS", 0)
    su._stop.clear()
    su._state.update(running=False)
    return fake


def _linked_model(c, name, n, source_id="3161"):
    m = c.post("/api/library/import", files={"file": (name, _stl(n), "application/octet-stream")}).json()
    r = c.post(f"/api/library/models/{m['id']}/source", json={"provider": "printables", "source_id": source_id, "images": False})
    assert r.status_code == 200, r.text
    return m["id"]


def _unlink_all(c, *ids):
    for i in ids:
        c.delete(f"/api/library/models/{i}/source")
        c.delete(f"/api/library/models/{i}")


def _check(c, sid):
    return c.post("/api/source-updates/check", json={"provider": "printables", "source_id": sid}).json()["outcome"]


def _changed(c, sid):
    """The flagged listings, for this test's own listing only (other tests leave their own links behind)."""
    return [l for l in c.get("/api/source-updates/changed").json()["listings"] if l["source_id"] == sid]


def test_first_look_records_a_baseline_then_changes_are_found(authed, sites):
    mid = _linked_model(authed, "upd_a.stl", 811, "88101")
    try:
        assert _check(authed, "88101") == "baseline" and _changed(authed, "88101") == []
        assert _check(authed, "88101") == "unchanged"
        changed = copy.deepcopy(PRINTABLES_PRINT)
        changed["data"]["print"]["description"] = "<p>Now with a new description.</p>"
        changed["data"]["print"]["tags"].append({"name": "Fresh"})
        sites.printables_print = changed
        assert _check(authed, "88101") == "changed"
        listing = _changed(authed, "88101")[0]
        assert listing["provider"] == "printables" and set(listing["changes"]) == {"description", "tags"}
        assert [m["id"] for m in listing["models"]] == [mid] and listing["label"] == "Printables"
    finally:
        _unlink_all(authed, mid)


def test_new_or_changed_files_are_noticed(authed, sites):
    mid = _linked_model(authed, "upd_files.stl", 812, "88102")
    try:
        _check(authed, "88102")
        files = copy.deepcopy(PRINTABLES_FILES)
        files["data"]["print"]["stls"].append({"id": "999", "name": "extra.stl", "fileSize": 5})
        sites.printables_files = files
        assert _check(authed, "88102") == "changed"
        assert _changed(authed, "88102")[0]["changes"] == ["files"]
    finally:
        _unlink_all(authed, mid)


def test_a_change_stays_flagged_until_refreshed_or_dismissed(authed, sites):
    mid = _linked_model(authed, "upd_flag.stl", 813, "88103")
    try:
        _check(authed, "88103")
        changed = copy.deepcopy(PRINTABLES_PRINT)
        changed["data"]["print"]["name"] = "3D BENCHY v2"
        changed["data"]["print"]["description"] = "<p>Version two.</p>"
        sites.printables_print = changed
        _check(authed, "88103")
        _check(authed, "88103")
        assert len(_changed(authed, "88103")) == 1
        r = authed.post("/api/source-updates/refresh", json={"provider": "printables", "source_id": "88103"})
        assert r.status_code == 200 and r.json() == {"models": 1}
        model = authed.get(f"/api/library/models/{mid}").json()
        assert model["source_title"] == "3D BENCHY v2" and "Version two." in model["source_description"]
        assert _changed(authed, "88103") == [] and _check(authed, "88103") == "unchanged"
        again = copy.deepcopy(changed)
        again["data"]["print"]["license"] = {"name": "Other", "abbreviation": "CC-BY"}
        sites.printables_print = again
        assert _check(authed, "88103") == "changed"
        assert authed.post("/api/source-updates/dismiss", json={"provider": "printables", "source_id": "88103"}).status_code == 200
        assert _changed(authed, "88103") == [] and _check(authed, "88103") == "unchanged"
    finally:
        _unlink_all(authed, mid)


def test_refresh_leaves_your_own_edits_to_designer_and_license(authed, sites):
    mid = _linked_model(authed, "upd_edits.stl", 814, "88104")
    try:
        authed.patch(f"/api/library/models/{mid}", json={"designer": "My Own Edit", "notes": "my notes"})
        _check(authed, "88104")
        authed.post("/api/source-updates/refresh", json={"provider": "printables", "source_id": "88104"})
        model = authed.get(f"/api/library/models/{mid}").json()
        assert model["designer"] == "My Own Edit" and model["notes"] == "my notes"
    finally:
        _unlink_all(authed, mid)


def test_models_from_the_same_listing_are_looked_at_once(authed, sites):
    a = _linked_model(authed, "upd_pack_a.stl", 815, "88105")
    b = _linked_model(authed, "upd_pack_b.stl", 816, "88105")
    try:
        before = len([c for c in sites.calls if "api.printables.com" in c])
        assert _check(authed, "88105") == "baseline"
        # one details request + one file-list request for the listing, not per model
        assert len([c for c in sites.calls if "api.printables.com" in c]) - before == 2
        changed = copy.deepcopy(PRINTABLES_PRINT)
        changed["data"]["print"]["description"] = "<p>changed pack</p>"
        sites.printables_print = changed
        _check(authed, "88105")
        listing = _changed(authed, "88105")[0]
        assert sorted(m["id"] for m in listing["models"]) == sorted([a, b])
        assert authed.post("/api/source-updates/refresh", json={"provider": "printables", "source_id": "88105"}).json() == {"models": 2}
    finally:
        _unlink_all(authed, a, b)


def test_unlinking_forgets_what_was_learned(authed, sites):
    mid = _linked_model(authed, "upd_unlink.stl", 817, "88106")
    try:
        _check(authed, "88106")
        changed = copy.deepcopy(PRINTABLES_PRINT)
        changed["data"]["print"]["name"] = "Renamed"
        sites.printables_print = changed
        _check(authed, "88106")
        authed.delete(f"/api/library/models/{mid}/source")
        assert _changed(authed, "88106") == []
        relinked = authed.post(f"/api/library/models/{mid}/source", json={"provider": "printables", "source_id": "88106", "images": False})
        assert relinked.status_code == 200
        assert _check(authed, "88106") == "baseline"                      # a new link starts fresh
    finally:
        _unlink_all(authed, mid)


def test_a_site_error_is_reported_and_changes_nothing(authed, sites):
    mid = _linked_model(authed, "upd_err.stl", 818, "88107")
    try:
        _check(authed, "88107")
        sites.printables_down = True
        r = authed.post("/api/source-updates/check", json={"provider": "printables", "source_id": "88107"})
        assert r.status_code == 502
        assert _changed(authed, "88107") == []
    finally:
        _unlink_all(authed, mid)


@pytest.mark.parametrize("payload", [{}, {"provider": "nowhere", "source_id": "1"}, {"provider": "printables", "source_id": "abc"}])
def test_bad_listings_are_refused(authed, payload):
    for route in ("check", "dismiss", "refresh"):
        assert authed.post(f"/api/source-updates/{route}", json=payload).status_code == 400


def test_comparing_ignores_parts_that_could_not_be_read():
    old = {"title": "a", "description": "b", "tags": "c", "images": "d", "license": "e", "files": None}
    new = dict(old, files="x")
    assert su.changed_parts(old, new) == []
    assert su.changed_parts(dict(old, files="y"), new) == ["files"]
    assert su.changed_parts(old, dict(old, title="z", images="q")) == ["title", "images"]


def _wait(c, tries=60):
    for _ in range(tries):
        status = c.get("/api/source-updates/status").json()
        if not status["running"]:
            return status
        time.sleep(0.1)
    raise AssertionError("the check did not finish")


def test_the_job_checks_every_listing_once_and_counts_changes(authed, sites):
    a = _linked_model(authed, "upd_job_a.stl", 819, "88110")
    try:
        assert authed.post("/api/source-updates/start", json={}).status_code == 200
        first = _wait(authed)
        assert first["checked"] >= 1 and first["changed"] == 0 and first["message"] == "Finished."
        changed = copy.deepcopy(PRINTABLES_PRINT)
        changed["data"]["print"]["name"] = "Job change"
        sites.printables_print = changed
        authed.post("/api/source-updates/start", json={})
        assert _wait(authed)["checked"] == 0            # looked at a moment ago: left alone
        authed.post("/api/source-updates/start", json={"force": True})
        second = _wait(authed)
        assert second["changed"] >= 1 and any(l["provider"] == "printables" for l in _changed(authed, "88110"))
    finally:
        _unlink_all(authed, a)


def test_the_job_stops_when_a_site_rate_limits(authed, sites, monkeypatch):
    a = _linked_model(authed, "upd_job_rl.stl", 820, "88111")
    try:
        def limited(*args, **kwargs):
            raise sources.SourceError("Printables is rate limiting requests; try again in a few minutes")
        monkeypatch.setattr(su, "current_fingerprint", limited)
        authed.post("/api/source-updates/start", json={"force": True})
        status = _wait(authed)
        assert "rate limiting" in status["message"] and status["errors"] == 1
    finally:
        _unlink_all(authed, a)


def test_only_one_job_runs_at_a_time(authed, sites):
    su._state["running"] = True
    try:
        assert authed.post("/api/source-updates/start", json={}).status_code == 409
    finally:
        su._state["running"] = False


def test_update_checks_need_a_login(authed):
    authed.post("/api/auth/logout")
    try:
        assert authed.get("/api/source-updates/changed").status_code == 401
        assert authed.post("/api/source-updates/start", json={}).status_code == 401
    finally:
        from conftest import ensure_authenticated
        ensure_authenticated(authed)
