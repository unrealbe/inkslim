from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conversion.config import ConversionConfig
from conversion.models import (
    JobStatus,
    PageMetrics,
    PageResult,
    QualityStatus,
    TextBlock,
    TextSource,
)
from conversion.storage import JobNotFoundError, JobStore
from conversion.worker import ConversionWorker, JobExecutionResult


def make_page(page_number: int, text: str = "Sample OCR text") -> PageResult:
    return PageResult(
        page_number=page_number,
        width=612,
        height=792,
        blocks=[
            TextBlock(
                text=text,
                bbox=(0.1, 0.1, 0.9, 0.2),
                block_type="paragraph",
                confidence=96.5,
                source=TextSource.TESSERACT,
                line_number=1,
                paragraph_number=1,
            )
        ],
        source=TextSource.TESSERACT,
        quality=QualityStatus.HIGH,
        metrics=PageMetrics(
            mean_ocr_confidence=96.5,
            meaningful_character_ratio=0.98,
            suspicious_character_ratio=0.0,
            single_character_word_ratio=0.02,
        ),
    )


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "data" / "jobs.sqlite3", tmp_path / "data" / "jobs")


def make_job(store: JobStore, tmp_path: Path, name: str = "scan.pdf"):
    source = tmp_path / name
    source.write_bytes(b"%PDF-1.7 fake test input")
    return store.create_job(source, name, language="nld+eng")


def test_page_result_round_trip_preserves_sources_and_metrics():
    page = make_page(12, "A paragraph from the scan")

    restored = PageResult.from_dict(page.to_dict())

    assert restored == page
    assert restored.source is TextSource.TESSERACT
    assert restored.quality is QualityStatus.HIGH
    assert restored.metrics.mean_ocr_confidence == pytest.approx(96.5)


def test_page_result_rejects_invalid_bbox_and_metrics():
    with pytest.raises(ValueError, match="normalized"):
        TextBlock(
            text="bad box",
            bbox=(0.0, 0.0, 1.1, 1.0),
            block_type="text",
            confidence=None,
            source=TextSource.NATIVE,
        )

    with pytest.raises(ValueError, match="between 0 and 100"):
        PageMetrics(native_text_score=101)


def test_page_result_rejects_unknown_checkpoint_version():
    value = make_page(1).to_dict()
    value["schema_version"] = 99
    with pytest.raises(ValueError, match="schema version"):
        PageResult.from_dict(value)


def test_create_job_persists_input_and_workspace_layout(store, tmp_path):
    job = make_job(store, tmp_path, "My scanned book.pdf")
    workspace = store.workspace(job.id)

    assert job.status is JobStatus.QUEUED
    assert job.input_name == "My scanned book.pdf"
    assert Path(job.input_path).read_bytes() == b"%PDF-1.7 fake test input"
    assert workspace.input_dir.is_dir()
    assert workspace.pages_dir.is_dir()
    assert workspace.rendered_dir.is_dir()
    assert workspace.output_dir.is_dir()
    manifest = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
    assert manifest["id"] == job.id
    assert manifest["language"] == "nld+eng"


def test_create_job_rejects_non_pdf(store, tmp_path):
    source = tmp_path / "book.epub"
    source.write_bytes(b"not a PDF")
    with pytest.raises(ValueError, match="PDF input only"):
        store.create_job(source, source.name)


def test_checkpoint_is_atomic_and_first_missing_page_resumes(store, tmp_path):
    job = make_job(store, tmp_path)
    workspace = store.workspace(job.id)

    page1_path = workspace.save_page_result(make_page(1))
    workspace.save_page_result(make_page(2))

    assert page1_path.name == "000001.json"
    assert workspace.load_page_result(1).blocks[0].text == "Sample OCR text"
    assert workspace.completed_page_count(4) == 2
    assert workspace.first_missing_page(4) == 3
    assert not list(workspace.pages_dir.glob("*.tmp"))


def test_corrupt_checkpoint_is_treated_as_missing(store, tmp_path):
    job = make_job(store, tmp_path)
    workspace = store.workspace(job.id)
    workspace.pages_dir.mkdir(parents=True, exist_ok=True)
    workspace.page_result_path(1).write_text("{broken", encoding="utf-8")

    assert workspace.load_page_result(1) is None
    assert workspace.first_missing_page(2) == 1


def test_claim_is_atomic_across_store_instances(store, tmp_path):
    first_job = make_job(store, tmp_path, "first.pdf")
    second_job = make_job(store, tmp_path, "second.pdf")
    other_store = JobStore(store.database_path, store.jobs_dir)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda args: args[0].claim_next_job(args[1]),
                [(store, "worker-a"), (other_store, "worker-b")],
            )
        )

    claimed_ids = {job.id for job in claims if job is not None}
    assert claimed_ids == {first_job.id, second_job.id}
    assert len(claimed_ids) == 2


def test_interrupted_job_resumes_after_last_valid_page(store, tmp_path):
    job = make_job(store, tmp_path)

    # Simulate a worker that stopped unexpectedly after committing page 1.
    claimed = store.claim_next_job("worker-before-restart")
    assert claimed is not None
    store.update_progress(
        job.id,
        status=JobStatus.OCR,
        current_page=1,
        total_pages=3,
        completed_pages=1,
        message="OCR page 1",
    )
    workspace = store.workspace(job.id)
    workspace.save_page_result(make_page(1, "Already checkpointed"))

    restarted_store = JobStore(store.database_path, store.jobs_dir)
    assert restarted_store.recover_interrupted_jobs() == 1
    assert restarted_store.get_job(job.id).status is JobStatus.QUEUED

    processed_pages: list[int] = []

    def resume_processor(resumed_job, resumed_workspace, job_store):
        start_page = resumed_workspace.first_missing_page(3)
        for page_number in range(start_page, 4):
            assert not job_store.is_cancel_requested(resumed_job.id)
            resumed_workspace.save_page_result(make_page(page_number))
            processed_pages.append(page_number)
            job_store.update_progress(
                resumed_job.id,
                status=JobStatus.OCR,
                current_page=page_number,
                total_pages=3,
                completed_pages=page_number,
                message=f"OCR page {page_number}",
            )
        output = resumed_workspace.output_dir / "book.epub"
        report = resumed_workspace.output_dir / "report.json"
        output.write_bytes(b"dummy epub")
        report.write_text("{}", encoding="utf-8")
        return JobExecutionResult(output, report)

    worker = ConversionWorker(restarted_store, resume_processor, worker_id="worker-after-restart")
    assert worker.run_once() is True
    finished = restarted_store.get_job(job.id)

    assert processed_pages == [2, 3]
    assert finished.status is JobStatus.COMPLETED
    assert finished.completed_pages == 3
    assert Path(finished.output_path).read_bytes() == b"dummy epub"
    assert Path(finished.report_path).read_text(encoding="utf-8") == "{}"


def test_worker_marks_processor_errors_without_exposing_traceback(store, tmp_path):
    job = make_job(store, tmp_path)

    def broken_processor(_job, _workspace, _store):
        raise RuntimeError("internal path /private/storage details")

    worker = ConversionWorker(store, broken_processor)
    assert worker.run_once() is True
    failed = store.get_job(job.id)

    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "CONVERSION_FAILED"
    assert "private/storage" not in failed.error_message


def test_cancel_queued_job_immediately_and_active_job_cooperatively(store, tmp_path):
    queued = make_job(store, tmp_path, "queued.pdf")
    assert store.request_cancel(queued.id) is True
    assert store.get_job(queued.id).status is JobStatus.CANCELLED

    active = make_job(store, tmp_path, "active.pdf")
    active_claim = store.claim_next_job("worker")
    assert active_claim is not None and active_claim.id == active.id
    assert store.request_cancel(active.id) is True
    assert store.is_cancel_requested(active.id) is True
    assert store.get_job(active.id).status is JobStatus.ANALYZING


def test_worker_honours_cancellation_requested_during_processing(store, tmp_path):
    job = make_job(store, tmp_path)

    def cancel_processor(current_job, _workspace, job_store):
        job_store.request_cancel(current_job.id)

    worker = ConversionWorker(store, cancel_processor)
    assert worker.run_once() is True
    assert store.get_job(job.id).status is JobStatus.CANCELLED


def test_config_defaults_and_single_worker_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("INKSLIM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_OCR_PAGES", "0")
    monkeypatch.setenv("OCR_WORKERS", "1")
    config = ConversionConfig.from_env()

    assert config.database_path == tmp_path / "jobs.sqlite3"
    assert config.jobs_dir == tmp_path / "jobs"
    assert config.max_ocr_pages == 0
    assert config.ocr_timeout_page == 120

    monkeypatch.setenv("OCR_WORKERS", "2")
    with pytest.raises(ValueError, match="must remain 1"):
        ConversionConfig.from_env()


def test_missing_job_raises_not_found(store):
    with pytest.raises(JobNotFoundError):
        store.request_cancel("00000000-0000-0000-0000-000000000000")


def test_completion_honours_cancellation_requested_at_the_same_time(store, tmp_path):
    job = make_job(store, tmp_path)
    assert store.claim_next_job("worker") is not None
    store.request_cancel(job.id)

    completed = store.complete_job(job.id, output_path=tmp_path / "book.epub")

    assert completed is False
    assert store.get_job(job.id).status is JobStatus.CANCELLED
