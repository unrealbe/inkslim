from __future__ import annotations

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
    assert 'images included in the EPUB' in html
    assert 'files.input_size_bytes' in html
    assert 'completed · PDF' in html


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
