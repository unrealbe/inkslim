from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

import app


def test_conversion_ui_exposes_upload_language_and_job_controls():
    app.AUTH_USER = app.AUTH_PASS = ""
    response = app.app.test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert 'id="processing-action"' in html
    assert 'value="shrink"' in html
    assert 'value="convert"' in html
    assert 'id="conversion-options"' in html
    assert html.index('id="operation-card"') < html.index('id="drop"')
    assert 'id="selection-panel"' in html
    assert 'id="selected-files"' in html
    assert 'id="start-processing"' in html
    assert 'Start processing' in html
    assert 'href="#operation-card"' in html
    assert 'href="#results"' in html
    assert 'id="empty-history"' in html
    assert 'value="nld+eng"' not in html
    assert 'value="fra+eng"' not in html
    assert 'id="conversion-language"' in html
    assert 'id="report-panel"' in html
    assert 'Print / Save as PDF' in html
    assert 'View report' in html
    assert 'JSON report' in html
    assert 'id="conversion-drop"' not in html
    assert 'id="conversion-file"' not in html
    assert 'value="auto"' not in html
    assert 'value="eng" selected' in html
    assert '/static/dashboard.css' in html
    assert 'class="sidebar"' in html
    assert 'Your e-book workspace' in html
    assert 'value="nld"' in html
    assert 'value="eng"' in html
    assert 'value="fra"' in html
    assert 'value="deu"' in html
    assert 'id="conversion-list"' in html
    assert 'id="clear-conversion-history"' in html
    assert 'PDF-to-EPUB history' in html
    assert 'loadConversionHistory()' in html
    assert 'fetch("/api/conversions?limit=20"' in html
    assert 'addDeleteAction(item,job)' in html
    assert 'Permanently delete this conversion' in html
    assert 'Clear completed, failed and cancelled conversions' in html
    assert 'method:"DELETE"' in html
    assert 'Download EPUB' in html
    assert 'JSON report' in html
    assert '"Pages with results"' in html
    assert 'warning.page_numbers.join(", ")' in html
    assert 'warning.message || warning.code' in html
    assert 'imageStats.embedded_in_epub' in html
    assert 'imageStats.optimization_saved_bytes' in html
    assert 'report.document_structure' in html
    assert 'EPUB structure check' in html
    assert 'Possible repeated headings: ' in html
    assert 'duplicateHeadings.count + " · Source pages " + duplicatePages.join(", ")' in html
    assert '...(Array.isArray(structure.warnings) ? structure.warnings : [])' in html
    assert '(warning.message || warning.code || "Content warning")' in html
    assert 'POSSIBLE_DUPLICATE_HEADINGS' not in html
    assert 'Possible printed page-number artefacts' in html
    assert 'images included in the EPUB' in html
    assert 'files.input_size_bytes' in html
    assert 'completed · PDF' in html



def test_quality_report_renderer_displays_structure_warning_as_user_facing_text():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the browser-side report renderer")

    app.AUTH_USER = app.AUTH_PASS = ""
    html = app.app.test_client().get("/").get_data(as_text=True)
    match = re.search(
        r"function showQualityReport\(url\) \{.*?\n\}\nfunction renderConversionJob",
        html,
        re.DOTALL,
    )
    assert match is not None

    report = {
        "job": {"input_name": "sample.pdf", "total_pages": 4},
        "summary": {"pages_with_results": 4, "total_pages": 4, "decisions": {}, "result_sources": {}},
        "content_quality": {"text_page_coverage": 1, "pages_with_text": 4, "pages_without_text": 0},
        "images": {},
        "document_structure": {
            "status": "available",
            "reading_order": {"status": "ok", "out_of_order_transitions": 0},
            "duplicate_headings": {
                "count": 1,
                "occurrences": [{"occurrence_count": 2, "page_numbers": [2, 4]}],
            },
            "possible_page_number_artifacts": {"count": 0, "page_numbers": []},
            "warnings": [{
                "code": "POSSIBLE_DUPLICATE_HEADINGS",
                "message": "Identical heading text occurs more than once; review whether repeated headings are intentional.",
            }],
        },
        "output": {},
    }
    renderer = match.group(0).split("\nfunction renderConversionJob", 1)[0]
    script = f"""
class Element {{
  constructor(tag) {{ this.tag = tag; this.children = []; this.style = {{}}; this._text = ""; }}
  append(...items) {{ this.children.push(...items); }}
  appendChild(item) {{ this.children.push(item); }}
  replaceChildren(...items) {{ this.children = [...items]; this._text = ""; }}
  scrollIntoView() {{}}
  addEventListener() {{}}
  set textContent(value) {{ this._text = String(value); this.children = []; }}
  get textContent() {{ return this._text + this.children.map(item => item.textContent || "").join(" "); }}
}}
const panel = new Element("section");
const document = {{ createElement: tag => new Element(tag) }};
const window = {{ print() {{}} }};
const $ = () => panel;
const fmt = value => String(value) + " bytes";
const report = {json.dumps(report)};
const fetch = async () => ({{ ok: true, json: async () => report }});
{renderer}
showQualityReport("/report");
setImmediate(() => {{
  const rendered = panel.textContent;
  if (!rendered.includes("Possible repeated headings: 1 · Source pages 2, 4")) {{
    throw new Error("Repeated-heading summary was not rendered: " + rendered);
  }}
  if (!rendered.includes("Identical heading text occurs more than once")) {{
    throw new Error("Structure warning message was not rendered: " + rendered);
  }}
  if (rendered.includes("POSSIBLE_DUPLICATE_HEADINGS")) {{
    throw new Error("Internal warning code leaked into the UI");
  }}
}});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert result.returncode == 0


def test_existing_shrink_interface_remains_available():
    app.AUTH_USER = app.AUTH_PASS = ""
    response = app.app.test_client().get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'id="drop"' in html
    assert 'id="file"' in html
    assert html.count('type="file"') == 1
    assert 'id="epub-settings"' in html
    assert 'id="pdf-settings"' in html
    assert 'xhr.open("POST","shrink")' in html


def test_dashboard_stylesheet_is_served():
    app.AUTH_USER = app.AUTH_PASS = ""
    response = app.app.test_client().get("/static/dashboard.css")
    assert response.status_code == 200
    css = response.get_data(as_text=True)
    assert ".app-shell" in css
    assert ".sidebar" in css
    assert "@media(max-width:760px)" in css



def test_version_endpoint_exposes_release_and_commit_metadata(monkeypatch):
    monkeypatch.setattr(app, "APP_VERSION", "0.1.0-dev")
    monkeypatch.setattr(app, "APP_COMMIT", "abcdef1234567890")
    response = app.app.test_client().get("/api/version")
    assert response.status_code == 200
    assert response.get_json() == {
        "version": "0.1.0-dev",
        "commit": "abcdef1234567890",
    }


def test_conversion_progress_status_targets_visible_status_and_shows_percentage():
    app.AUTH_USER = app.AUTH_PASS = ""
    response = app.app.test_client().get("/")
    html = response.get_data(as_text=True)
    assert 'meta.className = "meta job-status"' in html
    assert 'item.querySelector(".job-status")' in html
    assert 'const percentage = Math.round((completedPages / totalPages) * 100);' in html
    assert 'progress.value = percentage;' in html
    assert 'completedPages + "/" + totalPages + " pages · " + percentage + "%"' in html
    assert 'progress.setAttribute("aria-label","Conversion progress: " + percentage + "%")' in html
    assert 'progress.setAttribute("aria-label","Conversion progress")' in html
    assert 'Math.max(0,Math.min(Number(details.completed_pages) || 0,Number(details.total_pages)))' in html
