"""The front end has no JS test runner, so guard the cheapest class of break: a
script that looks up an element id that nothing defines."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_every_looked_up_id_exists():
    html, js = _read("index.html"), _read("app.js")
    defined = set(re.findall(r'\bid="([^"$]+)"', html)) | set(re.findall(r'\bid="([^"$]+)"', js))
    # ids written as id="viewer-live-color" inside JS templates, or set as control.id = '...'
    defined |= set(re.findall(r"\.id\s*=\s*'([^']+)'", js))
    looked_up = set(re.findall(r"""\$\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)""", js))
    looked_up |= set(re.findall(r"""getElementById\(\s*['"]([A-Za-z0-9_-]+)['"]\s*\)""", js))
    missing = sorted(looked_up - defined)
    assert not missing, f"app.js looks up ids that no HTML or template defines: {missing}"


def test_pages_and_routes_line_up():
    html, js = _read("index.html"), _read("app.js")
    for section in ("page-model", "page-project", "project-page-body", "model-title"):
        assert f'id="{section}"' in html, section
    # every nav tab has a section and a loader
    tabs = re.findall(r'data-tab="([a-z]+)"', html)
    assert tabs
    for tab in tabs:
        assert f'id="tab-{tab}"' in html, f"no section for tab {tab}"
        assert re.search(rf"\b{tab}:\s*\(\)\s*=>", js), f"no route loader for tab {tab}"
    # the old popup is gone
    assert "viewer-modal" not in html and "openViewer" not in js
