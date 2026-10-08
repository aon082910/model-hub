"""The 'match my whole library' job and its review queue.

The sites are faked (see test_sources.py). The fake answers per search term, so
each test can decide which models have matches. The job looks at every
unlinked model in the shared test database, so assertions are about the
specific models a test created, never about totals.
"""
import json
import time

import httpx
import pytest

from app import source_match_jobs as jobs
from app import sources
from test_sources import MAKERWORLD_SEARCH, PRINTABLES_PRINT, PRINTABLES_SEARCH, FakeSites


class SearchSites(FakeSites):
    """Like FakeSites, but searches only find things for 'benchy' / 'gearbox'."""

    def __init__(self):
        super().__init__()
        self.searches = []
        self.gearbox_only_on = None      # e.g. 'printables': the one site that knows 'gearbox'
        self.detail_fails = False        # listing detail lookups fail (the search still works)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.printables.com":
            body = json.loads(request.content)
            if "searchPrints2" not in body["query"] and self.detail_fails:
                return httpx.Response(503)
            if "searchPrints2" in body["query"]:
                term = body["variables"]["q"].lower()
                self.searches.append(("printables", term))
                if self.printables_down:
                    return httpx.Response(503)
                if "benchy" in term:
                    return httpx.Response(200, json=PRINTABLES_SEARCH)
                if "gearbox" in term and self.gearbox_only_on in (None, "printables"):
                    item = {"id": "77", "name": "Planetary Gearbox", "slug": "planetary-gearbox",
                            "user": {"publicUsername": "Gear Guy"}, "license": {"abbreviation": "CC-BY"}, "image": None}
                    return httpx.Response(200, json={"data": {"result": {"items": [item]}}})
                return httpx.Response(200, json={"data": {"result": {"items": []}}})
        if host == "api.sketchfab.com" and request.url.path.endswith("/search"):
            self.searches.append(("sketchfab", request.url.params["q"].lower()))
            return httpx.Response(self.sketchfab_status, json={"results": []})
        if host == "api.bambulab.com" and "/search-service/" in str(request.url):
            term = request.url.params["keyword"].lower()
            self.searches.append(("makerworld", term))
            if self.makerworld_status != 200:
                return httpx.Response(self.makerworld_status)
            return httpx.Response(200, json=MAKERWORLD_SEARCH if "benchy" in term else {"total": 0, "hits": []})
        return super().__call__(request)


@pytest.fixture()
def sites(monkeypatch):
    fake = SearchSites()
    monkeypatch.setattr(sources, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(jobs, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", 0)
    jobs._stop.clear()
    jobs._state.update(running=False)
    return fake


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


def _model(c, filename, n):
    r = c.post("/api/library/import", files={"file": (filename, _stl(n), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _run(*model_ids, recheck=False, auto=None):
    """Run the job over just these models (the shared test database holds hundreds
    of others), or over everything when none are given."""
    # what start_job does before handing over to the thread
    jobs._state.update(running=True, total=0, checked=0, with_candidates=0, no_match=0, auto_linked=0, errors=0, message="")
    jobs.run_job(recheck_none=recheck, only_ids=list(model_ids) or None, auto_link_min=auto)
    return jobs.job_status()


def _queue(c, **params):
    r = c.get("/api/source-match/queue", params={"limit": 100, **params})
    assert r.status_code == 200, r.text
    return {item["model"]["id"]: item for item in r.json()["items"]}


def _state(model_id):
    from sqlmodel import Session
    from app.db import engine
    from app.models import SourceCandidate, SourceMatchState
    from sqlmodel import select
    with Session(engine) as s:
        state = s.get(SourceMatchState, model_id)
        count = len(s.exec(select(SourceCandidate).where(SourceCandidate.model_id == model_id)).all())
        return (state.status if state else None), count


def test_requires_auth(client):
    client.post("/api/auth/logout")
    assert client.get("/api/source-match/status").status_code == 401
    assert client.get("/api/source-match/queue").status_code == 401


def test_job_finds_candidates_and_records_the_rest(authed, sites):
    hit = _model(authed, "Benchy_v2.stl", 11)
    miss = _model(authed, "ZxqwvPlinth.stl", 12)
    nameless = _model(authed, "a1b2c3d4e5f6a7b8.stl", 13)

    status = _run(hit, miss, nameless)
    assert status["running"] is False and status["message"] == "Finished."

    assert _state(hit) == ("candidates", 2)               # 3D BENCHY (Printables) + Benchy Bambu Pla Basic (MakerWorld)
    assert _state(miss) == ("none", 0)
    assert _state(nameless) == ("none", 0)
    assert not any("a1b2c3d4" in term for _, term in sites.searches)      # a hash-like name is never searched for

    item = _queue(authed)[hit]
    assert item["candidates"][0]["score"] >= item["candidates"][-1]["score"]
    assert item["best_score"] == item["candidates"][0]["score"]
    assert {c["provider"] for c in item["candidates"]} == {"printables", "makerworld"}
    assert not any("vase" in c["title"].lower() for c in item["candidates"])        # below the score bar
    assert miss not in _queue(authed) and nameless not in _queue(authed)


def test_second_run_does_not_search_again(authed, sites):
    model = _model(authed, "Gearbox_plain.stl", 21)
    _run(model)
    first = len([s for s in sites.searches if "gearbox" in s[1]])
    assert first >= 1
    _run(model)
    assert len([s for s in sites.searches if "gearbox" in s[1]]) == first       # already checked

    # a model with no match is looked at again only when asked to
    nothing = _model(authed, "Qwxzyplatform.stl", 22)
    _run(nothing)
    count = len([s for s in sites.searches if "qwxzyplatform" in s[1]])
    _run(nothing)
    assert len([s for s in sites.searches if "qwxzyplatform" in s[1]]) == count
    _run(nothing, recheck=True)
    assert len([s for s in sites.searches if "qwxzyplatform" in s[1]]) > count
    assert model and nothing


def test_min_score_filter_and_ordering(authed, sites):
    good = _model(authed, "Benchy.stl", 31)
    weak = _model(authed, "Gearbox_x.stl", 32)
    _run(good, weak)
    everything = _queue(authed)
    assert good in everything
    strict = _queue(authed, min_score=0.99)
    assert weak not in strict
    scores = [i["best_score"] for i in authed.get("/api/source-match/queue?limit=100").json()["items"]]
    assert scores == sorted(scores, reverse=True)


def test_skip_removes_from_queue_and_future_runs(authed, sites):
    model = _model(authed, "Benchy_skipme.stl", 41)
    _run(model)
    assert model in _queue(authed)
    assert authed.post(f"/api/source-match/models/{model}/skip").json() == {"status": "skipped"}
    assert model not in _queue(authed)
    assert _state(model) == ("skipped", 0)
    before = len(sites.searches)
    _run(model, recheck=True)                                 # recheck covers 'nothing found' only, not skipped
    assert _state(model) == ("skipped", 0)
    assert not any("skipme" in term for _, term in sites.searches[before:])
    assert authed.post("/api/source-match/models/999999/skip").status_code == 404


def test_linking_a_candidate_clears_the_review_state(authed, sites):
    model = _model(authed, "Benchy_linkme.stl", 51)
    _run(model)
    candidate = _queue(authed)[model]["candidates"][0]
    r = authed.post(f"/api/library/models/{model}/source", json={
        "provider": candidate["provider"], "source_id": candidate["source_id"], "images": False})
    assert r.status_code == 200, r.text
    assert model not in _queue(authed)
    assert _state(model) == (None, 0)
    before = len(sites.searches)
    _run(model)
    assert not any("linkme" in term for _, term in sites.searches[before:])     # linked models are not searched


def test_deleting_a_model_clears_its_candidates(authed, sites):
    model = _model(authed, "Benchy_deleteme.stl", 61)
    _run(model)
    assert _state(model)[1] > 0
    assert authed.delete(f"/api/library/models/{model}").status_code == 200
    assert _state(model) == (None, 0)


def test_one_site_down_still_finds_matches_from_the_other(authed, sites):
    sites.printables_down = True
    model = _model(authed, "Benchy_partial.stl", 71)
    _run(model)
    state, count = _state(model)
    assert state == "candidates" and count >= 1
    providers = {c["provider"] for c in _queue(authed)[model]["candidates"]}
    assert providers == {"makerworld"}


def test_site_down_with_no_results_is_retried_not_recorded_as_no_match(authed, sites):
    sites.printables_down = True
    model = _model(authed, "Gearbox_retry.stl", 72)      # only Printables knows 'gearbox', and it is down
    _run(model)
    assert _state(model) == (None, 0)
    sites.printables_down = False
    _run(model)
    assert _state(model)[0] == "candidates"


def test_job_stops_when_rate_limited(authed, sites):
    sites.printables_down = True
    sites.makerworld_status = 429
    sites.sketchfab_status = 429
    model = _model(authed, "Benchy_limited.stl", 81)
    status = _run(model)
    assert "rate limiting" in status["message"] and status["running"] is False
    assert _state(model) == (None, 0)                     # nothing was recorded; a later run retries it


def test_job_stops_after_repeated_failures(authed, sites):
    sites.printables_down = True
    sites.makerworld_status = 500
    sites.sketchfab_status = 500
    ids = [_model(authed, f"Benchy_fail{i}.stl", 90 + i) for i in range(jobs.MAX_CONSECUTIVE_FAILURES + 2)]
    status = _run(*ids)
    assert "keep failing" in status["message"]
    assert status["errors"] == jobs.MAX_CONSECUTIVE_FAILURES


def test_stop_request_ends_the_job(authed, sites):
    model = _model(authed, "Benchy_stop.stl", 101)
    jobs._stop.set()
    status = _run(model)
    assert status["message"].startswith("Stopped") and status["checked"] == 0


def test_start_endpoint_runs_in_the_background(authed, sites, monkeypatch):
    model = _model(authed, "Benchy_endpoint.stl", 111)
    real = jobs._models_to_check
    # the endpoint starts the real thread; keep it to this test's model (the shared test library is large)
    monkeypatch.setattr(jobs, "_models_to_check", lambda recheck, only_ids=None: real(recheck, [model]))
    started = authed.post("/api/source-match/start", json={})
    assert started.status_code == 200, started.text
    for _ in range(100):                                   # up to ~10s for the thread to finish
        job = authed.get("/api/source-match/status").json()["job"]
        if not job["running"]:
            break
        time.sleep(0.1)
    assert job["running"] is False and job["message"] == "Finished."
    assert job["checked"] == 1 and job["with_candidates"] == 1
    assert model in _queue(authed)


def test_whole_library_selection_skips_linked_and_already_checked_models(authed, sites):
    fresh = _model(authed, "Whole_fresh.stl", 121)
    checked = _model(authed, "Benchy_whole_checked.stl", 122)
    skipped = _model(authed, "Whole_skipped.stl", 123)
    nothing = _model(authed, "Whole_nothing.stl", 124)
    linked = _model(authed, "Whole_linked.stl", 125)
    _run(checked, nothing)                                   # 'checked' has candidates, 'nothing' found none
    authed.post(f"/api/source-match/models/{skipped}/skip")
    authed.post(f"/api/library/models/{linked}/source", json={"provider": "printables", "source_id": "3161", "images": False})

    wanted = jobs._models_to_check(False)
    assert fresh in wanted
    assert not {checked, skipped, nothing, linked} & set(wanted)
    again = jobs._models_to_check(True)                      # recheck: 'nothing found' models come back, skipped ones do not
    assert nothing in again and checked not in again and skipped not in again and linked not in again


def test_start_while_running_is_refused(authed, sites):
    jobs._state.update(running=True)
    try:
        assert authed.post("/api/source-match/start", json={}).status_code == 409
    finally:
        jobs._state.update(running=False)


def test_summary_counts(authed, sites):
    status = authed.get("/api/source-match/status").json()
    s = status["summary"]
    assert set(s) == {"total", "linked", "unlinked", "waiting_review", "no_match", "skipped", "unchecked"}
    assert s["total"] == s["linked"] + s["unlinked"]
    assert s["unlinked"] >= s["waiting_review"] + s["no_match"] + s["skipped"]
    assert s["unchecked"] == s["unlinked"] - s["waiting_review"] - s["no_match"] - s["skipped"]
    assert status["job"]["running"] is False
    assert PRINTABLES_PRINT                                  # (imported fixture, kept for the shared fake)


# ---------- auto-linking clear winners ----------

def test_clear_winner_rules():
    ranked = lambda *scores: [{"score": s} for s in scores]
    assert jobs.clear_winner(ranked(0.95, 0.60), 0.9) is True
    assert jobs.clear_winner(ranked(0.95), 0.9) is True                  # a lone strong match
    assert jobs.clear_winner(ranked(0.95, 0.90), 0.9) is False           # runner-up too close: ambiguous
    assert jobs.clear_winner(ranked(0.85, 0.10), 0.5) is False           # never below the 90% floor, whatever is asked
    assert jobs.clear_winner(ranked(0.92, 0.50), 0.95) is False          # under the asked-for threshold
    assert jobs.clear_winner(ranked(0.99, 0.50), None) is False         # auto-link is opt-in
    assert jobs.clear_winner([], 0.9) is False


def test_auto_link_links_only_clear_winners_and_marks_them(authed, sites):
    winner = _model(authed, "Benchy_v2.stl", 131)
    status = _run(winner, auto=0.9)
    assert status["auto_linked"] == 1 and status["with_candidates"] == 0
    m = authed.get(f"/api/library/models/{winner}").json()
    assert m["source_provider"] == "printables" and m["source_linked_by"] == "auto"
    assert m["designer"] == "Prusa Research" and json.loads(m["source_images"])
    assert winner not in _queue(authed)                                   # decided, so nothing left to review
    assert _state(winner) == (None, 0)

    listed = authed.get("/api/source-match/auto-linked").json()
    entry = next(e for e in listed if e["id"] == winner)
    assert entry["source_provider"] == "printables" and entry["source_title"] == "3D BENCHY"

    assert authed.post(f"/api/source-match/models/{winner}/confirm").json() == {"status": "confirmed"}
    assert winner not in {e["id"] for e in authed.get("/api/source-match/auto-linked").json()}
    assert authed.get(f"/api/library/models/{winner}").json()["source_linked_by"] == "manual"
    assert authed.post(f"/api/source-match/models/{winner}/confirm").status_code == 404      # no longer auto-linked


def test_without_the_option_nothing_is_auto_linked(authed, sites):
    model = _model(authed, "Benchy_manual_only.stl", 132)
    status = _run(model)
    assert status["auto_linked"] == 0 and status["with_candidates"] == 1
    assert authed.get(f"/api/library/models/{model}").json()["source_provider"] is None
    assert model in _queue(authed)


def test_auto_linked_model_can_be_unlinked(authed, sites):
    model = _model(authed, "Benchy_v3 (2).stl", 133)
    _run(model, auto=0.9)
    r = authed.delete(f"/api/library/models/{model}/source").json()
    assert r["source_provider"] is None and r["source_linked_by"] is None
    assert model not in {e["id"] for e in authed.get("/api/source-match/auto-linked").json()}


def test_auto_link_failure_keeps_the_candidates_for_review(authed, sites):
    model = _model(authed, "Benchy_v2 (3).stl", 134)
    sites.detail_fails = True
    _run(model, auto=0.9)
    assert authed.get(f"/api/library/models/{model}").json()["source_provider"] is None
    assert _state(model)[0] == "candidates"                              # fell back to asking a person


def test_start_endpoint_validates_the_auto_link_threshold(authed, sites):
    for bad in (0.5, 1.5, "lots", -1):
        assert authed.post("/api/source-match/start", json={"auto_link_min": bad}).status_code == 400, bad
