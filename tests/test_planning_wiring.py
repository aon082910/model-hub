"""The page has the places the new calendar, camera, estimate and gallery-link code writes into."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_the_calendar_has_its_tab_section_and_every_element_the_script_uses():
    assert 'data-tab="calendar"' in HTML and 'id="tab-calendar"' in HTML
    assert re.search(r"calendar:\s*\(\)\s*=>\s*loadCalendar\(\)", JS)
    for element in ("cal-prev", "cal-next", "cal-today", "cal-title", "cal-grid", "cal-day", "cal-plan-preview", "cal-plan-run", "cal-status", "cal-unplanned"):
        assert f'id="{element}"' in HTML, element
        assert f"#{element}" in JS, element


def test_settings_have_the_planning_hours_and_the_library_share_panel():
    for element in ("calendar-hours", "calendar-hours-save", "calendar-hours-status", "library-share-panel", "printer-snapshot"):
        assert f'id="{element}"' in HTML, element
    assert "renderSharePanel('#library-share-panel', 'library', 0)" in JS


def test_collections_can_be_shared_and_names_are_escaped_there():
    assert "renderSharePanel(`#collection-share-${b.dataset.id}`, 'collection'" in JS
    assert "${esc(c.name)}" in JS


def test_the_queue_and_model_page_use_the_learned_times():
    assert "/api/estimates/accuracy" in JS and "/api/estimates/${modelId}" in JS
    assert "planned_date" in JS and "queue-date" in JS


def test_the_ics_link_points_at_the_real_route():
    assert 'href="/api/calendar/export.ics"' in HTML


def test_failed_prints_have_their_controls():
    assert "print-outcome" in JS and "/api/prints/reasons" in JS and "failure_reason" in JS
    assert "queue-fail-save" in JS and "Why did it fail?" in JS
    assert "d.failures.by_reason" in JS and "Failed prints" in JS


def test_the_calendar_has_a_printer_choice_and_filament_warnings():
    for element in ("cal-printer", "cal-printer-label", "cal-short-note", "cal-printer-note", "cal-ics"):
        assert f'id="{element}"' in HTML, element
    assert "calPrinter" in JS and "short_count" in JS and "p.short" in JS


def test_photos_can_be_taken_with_a_phone_camera_where_prints_are_shown():
    assert 'capture="environment"' in JS and "cal-photo-input" in JS
    assert JS.count('class="print-photo-input"') >= 2                        # one to take a photo, one to choose from the gallery


def test_smithsonian_has_a_label_in_the_page():
    assert "smithsonian: 'Smithsonian 3D'" in JS


def test_failure_hints_slots_timelapses_and_week_repeat_have_their_controls():
    assert "hintText(" in JS and "/api/prints/hints" in JS and "hint-box" in JS
    assert 'id="f-failed"' in HTML and "failed_before" in JS and "'#f-failed'" in JS
    assert 'id="slots-panel"' in HTML and "renderSlotsPanel" in JS and "/api/slots" in JS and "queue-slot" in JS
    assert "printer-slot-count" in JS and "slot_count" in JS
    assert "timelapse-find" in JS and "timelapse-candidates" in JS and "timelapse_url" in JS
    assert "cal-repeat-go" in JS and "/api/calendar/copy" in JS


def test_bambu_maintenance_slicer_weekly_and_suggestions_have_their_controls():
    assert 'value="bambu"' in HTML and 'id="printer-serial"' in HTML and "syncPrinterForm" in JS and "slot-apply" in JS and "slot-sync" in JS
    assert 'id="maintenance-panel"' in HTML and "loadMaintenance" in JS and "/api/maintenance" in JS and "maint-done" in JS
    assert 'id="model-slicer-panel"' in HTML and "renderSlicerPanel" in JS and "/api/slicer-link/" in JS
    assert 'id="weekly-summary"' in HTML and "/api/settings/weekly-test" in JS
    assert "suggested-settings" in JS and "ps-use" in JS
    assert "timelapse-play" in JS and "archive: 'Internet Archive (Thingiverse)'" in JS


def test_stage_16_controls_are_in_the_page():
    assert "printer-pause" in JS and "printer-resume" in JS and "printer-cancel" in JS and "/control" in JS
    assert "printer-bed-save" in JS and "bed_x" in JS and "/api/fit/queue" in JS and "/api/fit/model/" in JS
    assert 'id="f-fits-printer"' in HTML and "fits_printer" in JS and "fillFitPrinters" in JS
    assert "queue-colours" in JS and "uses-from-file" in JS and "colours-save" in JS
    for element in ("offsite-kind", "offsite-path", "offsite-url", "offsite-user", "offsite-password", "offsite-keep", "offsite-save", "offsite-test", "offsite-now", "offsite-status"):
        assert f'id="{element}"' in HTML, element
    assert "/api/backup/offsite/test" in JS and "/api/backup/offsite/now" in JS


def test_stage_17_controls_are_in_the_page():
    assert 'data-tab="orders"' in HTML and 'id="tab-orders"' in HTML and re.search(r"orders:\s*\(\)\s*=>\s*loadOrders\(\)", JS)
    for element in ("order-customer", "order-create", "orders-list", "model-cost-panel", "cost-machine-hour", "cost-failure-pct", "cost-margin-pct", "cost-default-kg",
                    "spoolman-url", "spoolman-usage", "spoolman-save", "spoolman-test", "spoolman-import", "spoolman-export"):
        assert f'id="{element}"' in HTML, element
    assert "/api/costs/quote" in JS and "renderCostPanel" in JS and "/api/orders" in JS and "order-queue" in JS
    assert "/api/queue/suggestions" in JS and "queue-assign-all" in JS and "/api/spoolman/" in JS
