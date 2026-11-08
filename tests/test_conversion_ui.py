from __future__ import annotations

import app


def test_conversion_ui_exposes_upload_language_and_job_controls():
    app.AUTH_USER = app.AUTH_PASS = ""
    response = app.app.test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert 'id="conversion-panel"' in html
    assert 'id="conversion-file"' in html
    assert 'id="conversion-language"' in html
    assert 'value="auto"' in html
    assert 'value="nld"' in html
    assert 'value="eng"' in html
    assert 'value="fra"' in html
    assert 'value="deu"' in html
    assert 'id="conversion-list"' in html
    assert 'fetch("/api/conversions"' in html
    assert 'method:"DELETE"' in html
    assert 'Download EPUB' in html
    assert 'Quality report' in html


def test_existing_shrink_interface_remains_available():
    app.AUTH_USER = app.AUTH_PASS = ""
    response = app.app.test_client().get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'id="drop"' in html
    assert 'id="epub-settings"' in html
    assert 'id="pdf-settings"' in html
    assert 'xhr.open("POST","shrink")' in html
