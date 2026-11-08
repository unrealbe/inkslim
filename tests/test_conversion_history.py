from __future__ import annotations

import io
from pathlib import Path

import pytest

import app as webapp
from conversion.models import JobStatus
from conversion.storage import InvalidJobStateError, JobStore


@pytest.fixture
def history_client(tmp_path, monkeypatch):
    data_dir = tmp_path / "conversion-data"
    store = JobStore(data_dir / "jobs.sqlite3", data_dir / "jobs")
    monkeypatch.setitem(webapp.app.extensions, "conversion_store", store)
    webapp.AUTH_USER = webapp.AUTH_PASS = ""
    client = webapp.app.test_client()
    yield client, store, tmp_path
    webapp.app.extensions.pop("conversion_store", None)


def source_pdf(tmp_path: Path, name: str = "book.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return path


def make_terminal_job(store: JobStore, tmp_path: Path, status: JobStatus):
    job = store.create_job(source_pdf(tmp_path, f"{status.value}.pdf"), f"{status.value}.pdf")
    workspace = store.workspace(job.id)
    if status is JobStatus.CANCELLED:
        store.request_cancel(job.id)
    elif status is JobStatus.FAILED:
        claimed = store.claim_next_job("test-worker")
        assert claimed and claimed.id == job.id
        store.fail_job(job.id, "TEST_FAILURE", "Test failure")
    elif status is JobStatus.COMPLETED:
        claimed = store.claim_next_job("test-worker")
        assert claimed and claimed.id == job.id
        output = workspace.output_dir / "book.epub"
        output.write_bytes(b"test epub")
        store.complete_job(job.id, output_path=output)
    return job


@pytest.mark.parametrize(
    "status",
    [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED],
)
def test_delete_job_removes_terminal_row_and_workspace(history_client, status):
    _, store, tmp_path = history_client
    job = make_terminal_job(store, tmp_path, status)
    workspace = store.workspace(job.id)
    assert workspace.root.exists()

    assert store.delete_job(job.id) is True
    assert store.get_job(job.id) is None
    assert not workspace.root.exists()
    assert store.delete_job(job.id) is False


def test_delete_job_refuses_queued_or_active_jobs(history_client):
    _, store, tmp_path = history_client
    job = store.create_job(source_pdf(tmp_path), "book.pdf")

    with pytest.raises(InvalidJobStateError):
        store.delete_job(job.id)

    assert store.get_job(job.id) is not None
    assert store.workspace(job.id).root.exists()


def test_clear_terminal_jobs_preserves_active_jobs(history_client):
    _, store, tmp_path = history_client
    active = store.create_job(source_pdf(tmp_path, "active.pdf"), "active.pdf")
    cancelled = make_terminal_job(store, tmp_path, JobStatus.CANCELLED)
    failed = make_terminal_job(store, tmp_path, JobStatus.FAILED)

    assert store.clear_terminal_jobs() == 2
    assert store.get_job(active.id) is not None
    assert store.workspace(active.id).root.exists()
    assert store.get_job(cancelled.id) is None
    assert store.get_job(failed.id) is None
    assert store.clear_terminal_jobs() == 0


def test_history_api_lists_jobs_with_safe_metadata_and_no_store_headers(history_client):
    client, store, tmp_path = history_client
    job = store.create_job(source_pdf(tmp_path), "Original book.pdf")

    response = client.get("/api/conversions?limit=20")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"
    payload = response.get_json()[0]
    assert payload["id"] == job.id
    assert payload["short_id"] == job.id[:8]
    assert payload["input_name"] == "Original book.pdf"
    assert payload["links"]["delete"].endswith(f"/{job.id}/history")


def test_delete_history_api_refuses_active_and_deletes_terminal(history_client):
    client, store, tmp_path = history_client
    active = store.create_job(source_pdf(tmp_path, "active.pdf"), "active.pdf")
    active_response = client.delete(f"/api/conversions/{active.id}/history")
    assert active_response.status_code == 409
    assert store.get_job(active.id) is not None

    terminal = make_terminal_job(store, tmp_path, JobStatus.CANCELLED)
    response = client.delete(f"/api/conversions/{terminal.id}/history")
    assert response.status_code == 200
    assert response.get_json()["deleted"] is True
    assert store.get_job(terminal.id) is None


def test_clear_history_api_only_removes_terminal_jobs(history_client):
    client, store, tmp_path = history_client
    active = store.create_job(source_pdf(tmp_path, "active.pdf"), "active.pdf")
    make_terminal_job(store, tmp_path, JobStatus.CANCELLED)

    response = client.delete("/api/conversions/history")
    assert response.status_code == 200
    assert response.get_json()["removed"] == 1
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert store.get_job(active.id) is not None
