"""Data behind the model and project pages, and the project PDF export."""
import io

from pypdf import PdfReader


def _stl(n):
    return (f"solid t\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex {n} 0 0\n  vertex 0 {n} 0\n"
            " endloop\nendfacet\nendsolid t\n").encode()


def _model(c, name, n):
    r = c.post("/api/library/import", files={"file": (name, _stl(n), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def _pdf_text(response) -> str:
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join(page.extract_text() for page in reader.pages)


# ---------- model page data ----------

def test_model_full_collects_everything_the_page_shows(authed):
    model = _model(authed, "page_full.stl", 21)
    mid = model["id"]
    pid = authed.post("/api/projects", json={"name": "Page project"}).json()["id"]
    authed.post(f"/api/projects/{pid}/models/{mid}")
    cid = authed.post("/api/collections", json={"name": "Page collection"}).json()["id"]
    authed.post(f"/api/collections/{cid}/models/{mid}")
    authed.post(f"/api/tags/models/{mid}", json={"name": "Page-Tag"})
    authed.post("/api/queue", json={"model_id": mid, "estimated_grams": 12.5})

    full = authed.get(f"/api/library/models/{mid}/full").json()
    assert full["filename"] == "page_full.stl" and full["file_exists"] is True
    assert [t["name"] for t in full["tags"]] == ["page-tag"]
    assert full["projects"] == [{"id": pid, "name": "Page project", "status": "planning"}]
    assert full["collections"] == [{"id": cid, "name": "Page collection"}]
    assert full["queue"][0]["status"] == "queued" and full["queue"][0]["estimated_grams"] == 12.5
    assert full["duplicates"] == [] and "embedding" not in full
    assert authed.get("/api/library/models/999999/full").status_code == 404


def test_model_full_lists_duplicates_both_ways(authed):
    from sqlmodel import Session
    from app.db import engine
    from app.models import Model3D

    original = _model(authed, "page_dup_original.stl", 22)
    with Session(engine) as session:
        copy = Model3D(filename="page_dup_copy.stl", path="pages/page_dup_copy.stl", extension=".stl", size_bytes=1,
                       content_hash="page-dup-hash", is_duplicate_of=original["id"])
        session.add(copy)
        session.commit()
        session.refresh(copy)
        copy_id = copy.id
    assert [d["id"] for d in authed.get(f"/api/library/models/{original['id']}/full").json()["duplicates"]] == [copy_id]
    assert [d["id"] for d in authed.get(f"/api/library/models/{copy_id}/full").json()["duplicates"]] == [original["id"]]
    assert authed.get(f"/api/library/models/{copy_id}/full").json()["file_exists"] is False


def test_model_notes_and_fields_validate(authed):
    mid = _model(authed, "page_notes.stl", 23)["id"]
    r = authed.patch(f"/api/library/models/{mid}", json={"notes": "  print at 0.12mm  ", "designer": "Me", "license": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["notes"] == "print at 0.12mm" and body["designer"] == "Me" and body["license"] is None
    assert "tags" in body and "embedding" not in body
    assert authed.patch(f"/api/library/models/{mid}", json={"notes": 5}).status_code == 400
    assert authed.patch(f"/api/library/models/{mid}", json={"filename": "  "}).status_code == 400
    assert authed.patch(f"/api/library/models/{mid}", json={"filename": "renamed.stl"}).json()["filename"] == "renamed.stl"
    assert authed.patch(f"/api/library/models/{mid}", json={"notes": None}).json()["notes"] is None
    assert authed.patch("/api/library/models/999999", json={"notes": "x"}).status_code == 404


def test_tags_and_collections_can_be_removed_from_a_model(authed):
    mid = _model(authed, "page_remove.stl", 24)["id"]
    authed.post(f"/api/tags/models/{mid}", json={"name": "remove-me"})
    authed.post(f"/api/tags/models/{mid}", json={"name": "keep-me"})
    authed.delete(f"/api/tags/models/{mid}/remove-me")
    assert [t["name"] for t in authed.get(f"/api/library/models/{mid}/full").json()["tags"]] == ["keep-me"]

    cid = authed.post("/api/collections", json={"name": "Remove collection"}).json()["id"]
    authed.post(f"/api/collections/{cid}/models/{mid}")
    assert authed.delete(f"/api/collections/{cid}/models/{mid}").json() == {"status": "removed"}
    assert authed.get(f"/api/library/models/{mid}/full").json()["collections"] == []
    assert authed.delete(f"/api/collections/{cid}/models/{mid}").status_code == 200       # already gone: harmless
    assert authed.delete(f"/api/collections/999999/models/{mid}").status_code == 404


def test_project_models_carry_listing_info_for_the_page(authed):
    mid = _model(authed, "page_listing.stl", 25)["id"]
    authed.patch(f"/api/library/models/{mid}", json={"designer": "Ada", "license": "CC0", "source_url": "https://example.com/x"})
    pid = authed.post("/api/projects", json={"name": "Listing project"}).json()["id"]
    model = authed.post(f"/api/projects/{pid}/models/{mid}").json()["models"][0]
    assert (model["designer"], model["license"], model["source_url"]) == ("Ada", "CC0", "https://example.com/x")
    assert model["source_images"] == [] and model["source_provider"] is None


# ---------- project PDF ----------

def test_project_pdf_contains_the_project(authed):
    model = _model(authed, "pdf_model.stl", 26)
    authed.patch(f"/api/library/models/{model['id']}", json={"designer": "Grace Hopper", "license": "CC-BY", "source_url": "https://example.com/listing"})
    pid = authed.post("/api/projects", json={
        "name": "Löt Station – Rev 2", "description": "A soldering station with a printed holder",
        "notes": "Use 4 walls.\nCheck the µF rating of the caps.", "status": "building"}).json()["id"]
    authed.post(f"/api/projects/{pid}/models/{model['id']}")
    spool = authed.post("/api/filament", json={"material": "PETG", "brand": "PDF", "color": "Orange", "remaining_g": 40}).json()
    authed.post(f"/api/projects/{pid}/models/{model['id']}/filament", json={"filament_id": spool["id"], "grams": 55})
    for part in (
        {"name": "ESP32 board", "category": "electronics", "quantity": 2, "quantity_owned": 1, "unit_cost": 8.5, "purchase_url": "https://example.com/esp"},
        {"name": "M3x8 screw", "category": "parts", "quantity": 10, "quantity_owned": 10},
        {"name": "Flux pen", "category": "supplies", "quantity": 1, "unit_cost": 6, "notes": "lead free"},
    ):
        authed.post(f"/api/projects/{pid}/parts", json=part)

    r = authed.get(f"/api/projects/{pid}/export.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="project-l-t-station-rev-2.pdf"'
    assert r.content[:5] == b"%PDF-"

    text = _pdf_text(r)
    for expected in (
        "Löt Station – Rev 2", "building", "A soldering station with a printed holder", "Use 4 walls.", "µF rating",
        "pdf_model.stl", "by Grace Hopper", "CC-BY",
        "PETG PDF Orange: 55 g", "not enough",                       # 55 g planned from a 40 g spool
        "Electronics", "Parts & hardware", "Supplies", "ESP32 board", "M3x8 screw", "Flux pen", "lead free", "have all",
        "Estimated cost still to buy", "$14.50", "Page 1",
    ):
        assert expected in text, expected
    reader = PdfReader(io.BytesIO(r.content))
    links = [a.get_object().get("/A", {}).get("/URI") for page in reader.pages for a in (page.get("/Annots") or [])]
    assert "https://example.com/esp" in links and "https://example.com/listing" in links


def test_empty_project_pdf_is_still_valid(authed):
    pid = authed.post("/api/projects", json={"name": "Empty pdf project"}).json()["id"]
    r = authed.get(f"/api/projects/{pid}/export.pdf")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    text = _pdf_text(r)
    assert "Empty pdf project" in text and "No parts listed." in text


def test_long_parts_list_runs_onto_more_pages(authed):
    pid = authed.post("/api/projects", json={"name": "Long pdf project"}).json()["id"]
    parts = [{"name": f"Long list part {i:03d}", "category": ["electronics", "parts", "supplies"][i % 3], "quantity": i + 1,
              "unit_cost": 1.5, "notes": "a fairly long note about this part " * 2} for i in range(150)]
    assert authed.post(f"/api/projects/{pid}/parts/bulk", json={"parts": parts}).status_code == 200
    r = authed.get(f"/api/projects/{pid}/export.pdf")
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) > 2
    text = _pdf_text(r)
    assert "Long list part 000" in text and "Long list part 149" in text and f"Page {len(reader.pages)}" in text


def test_pdf_text_is_escaped_not_interpreted(authed):
    pid = authed.post("/api/projects", json={"name": "<b>bold</b> & <i>x</i>", "description": "<font color=red>raw</font> & more"}).json()["id"]
    authed.post(f"/api/projects/{pid}/parts", json={"name": "<para>tag</para>", "category": "parts", "purchase_url": "javascript:alert(1)"})
    r = authed.get(f"/api/projects/{pid}/export.pdf")
    assert r.status_code == 200
    text = _pdf_text(r)
    assert "<b>bold</b> & <i>x</i>" in text and "<font color=red>raw</font>" in text and "<para>tag</para>" in text
    reader = PdfReader(io.BytesIO(r.content))
    uris = [a.get_object().get("/A", {}).get("/URI") for page in reader.pages for a in (page.get("/Annots") or [])]
    assert not any(u and u.startswith("javascript:") for u in uris)       # only http(s) links are made clickable


def test_pdf_for_a_missing_project(authed):
    assert authed.get("/api/projects/999999/export.pdf").status_code == 404
    authed.post("/api/auth/logout")
    assert authed.get("/api/projects/1/export.pdf").status_code == 401
    from conftest import ensure_authenticated
    ensure_authenticated(authed)
