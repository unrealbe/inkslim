from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

import app as webapp
from conversion.epub.builder import Block, Chapter, EpubMetadata, build_epub
from conversion.models import JobStatus
from conversion.storage import JobStore


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    data_dir = tmp_path / "conversion-data"
    store = JobStore(data_dir / "jobs.sqlite3", data_dir / "jobs")
    monkeypatch.setitem(webapp.app.extensions, "conversion_store", store)
    webapp.AUTH_USER = webapp.AUTH_PASS = ""
    webapp.app.config["CONVERSION_MAX_PDF_PAGES"] = 20
    yield webapp.app.test_client()
    webapp.app.extensions.pop("conversion_store", None)


def pdf_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "sample.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, "A small PDF fixture with a little bit of text.")
    pdf.showPage()
    pdf.save()
    return path.read_bytes()


def submit(api_client, content: bytes, filename="sample.pdf", **fields):
    data = {"file": (io.BytesIO(content), filename), **fields}
    return api_client.post("/api/conversions", data=data, content_type="multipart/form-data")


def test_create_conversion_returns_queued_job_and_public_links(api_client, tmp_path):
    response = submit(api_client, pdf_bytes(tmp_path), language="nld+eng")

    assert response.status_code == 201
    body = response.get_json()
    assert body["status"] == "queued"
    assert body["language"] == "nld+eng"
    assert body["progress"]["completed_pages"] == 0
    assert body["links"]["self"].endswith(body["id"])
    assert "input_path" not in body
    assert "worker_id" not in body
    assert response.headers["Location"] == body["links"]["self"]


@pytest.mark.parametrize(
    ("content", "filename", "fields", "status"),
    [
        (b"not a pdf", "book.pdf", {}, 422),
        (b"%PDF-1.7", "book.txt", {}, 400),
        (b"", "empty.pdf", {}, 422),
        (b"%PDF-1.7", "book.pdf", {"language": "italian"}, 400),
    ],
)
def test_create_conversion_rejects_invalid_uploads(api_client, content, filename, fields, status):
    response = submit(api_client, content, filename, **fields)
    assert response.status_code == status


def test_status_and_unknown_job_ids(api_client, tmp_path):
    created = submit(api_client, pdf_bytes(tmp_path)).get_json()
    response = api_client.get(created["links"]["self"])
    assert response.status_code == 200
    assert response.get_json()["id"] == created["id"]

    assert api_client.get("/api/conversions/not-a-uuid").status_code == 404
    assert api_client.get("/api/conversions/00000000-0000-0000-0000-000000000000").status_code == 404


def test_cancel_queued_job_and_reject_repeat_after_terminal(api_client, tmp_path):
    created = submit(api_client, pdf_bytes(tmp_path)).get_json()
    url = created["links"]["self"]
    cancelled = api_client.delete(url)
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"
    assert api_client.delete(url).status_code == 409


def test_download_requires_completed_job(api_client, tmp_path):
    created = submit(api_client, pdf_bytes(tmp_path)).get_json()
    assert api_client.get(created["links"]["download"]).status_code == 409
    assert api_client.get(created["links"]["report"]).status_code == 409


def test_completed_job_downloads_only_validated_epub_and_json_report(api_client, tmp_path):
    created = submit(api_client, pdf_bytes(tmp_path), "My book.pdf").get_json()
    store = webapp.app.extensions["conversion_store"]
    job = store.get_job(created["id"])
    store.claim_next_job("test-worker")
    workspace = store.workspace(job.id)
    output = build_epub(
        workspace.output_dir / f"My-book-{job.id[:8]}.epub",
        EpubMetadata(title="My book", language="en"),
        [Chapter("Chapter 1", [Block("p", "A readable paragraph for the generated EPUB.")])],
    )
    report = workspace.output_dir / "quality-report.json"
    report.write_text(json.dumps({"schema_version": 2, "output": {"format": "epub"}}), encoding="utf-8")
    store.complete_job(job.id, output_path=output, report_path=report)

    downloaded_epub = api_client.get(created["links"]["download"])
    assert downloaded_epub.status_code == 200
    assert downloaded_epub.mimetype == "application/epub+zip"
    assert downloaded_epub.data[:2] == b"PK"
    assert downloaded_epub.headers["Cache-Control"] == "no-store, max-age=0"
    assert downloaded_epub.headers["Pragma"] == "no-cache"
    assert downloaded_epub.headers["Expires"] == "0"
    assert downloaded_epub.headers["Content-Disposition"].endswith(
        f"My-book-{created['id'][:8]}.epub"
    )

    downloaded_report = api_client.get(created["links"]["report"])
    assert downloaded_report.status_code == 200
    assert downloaded_report.mimetype == "application/json"
    assert json.loads(downloaded_report.data)["schema_version"] == 3
    assert downloaded_report.headers["Cache-Control"] == "no-store, max-age=0"
    assert downloaded_report.headers["Pragma"] == "no-cache"
    assert downloaded_report.headers["Expires"] == "0"


def test_completed_job_rejects_paths_outside_workspace(api_client, tmp_path):
    created = submit(api_client, pdf_bytes(tmp_path)).get_json()
    store = webapp.app.extensions["conversion_store"]
    job = store.get_job(created["id"])
    store.claim_next_job("test-worker")
    outside = tmp_path / "secret.epub"
    outside.write_bytes(b"not a book")
    store.complete_job(job.id, output_path=outside, report_path=outside)

    assert api_client.get(created["links"]["download"]).status_code == 404
    assert api_client.get(created["links"]["report"]).status_code == 404

def test_api_returns_507_when_storage_admission_quota_is_full(api_client, tmp_path):
    store = webapp.app.extensions["conversion_store"]
    store.max_storage_bytes = 1
    response = submit(api_client, pdf_bytes(tmp_path))
    assert response.status_code == 507
    assert response.get_json()["code"] == "STORAGE_QUOTA_EXCEEDED"
