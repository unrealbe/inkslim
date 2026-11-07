from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conversion.models import PageMetrics, PageResult, QualityStatus, TextBlock, TextSource
from conversion.pdf.models import PageAnalysis, PageDecision
from conversion.quality_report import build_quality_report
from conversion.storage import JobStore


def _analysis(page_number: int, decision: PageDecision) -> PageAnalysis:
    return PageAnalysis(
        page_number=page_number,
        width=612,
        height=792,
        character_count=120,
        word_count=22,
        image_count=1,
        has_native_text=decision is PageDecision.USE_NATIVE,
        native_text_quality=0.95 if decision is PageDecision.USE_NATIVE else None,
        decision=decision,
        warnings=["NATIVE_OCR_SIMILARITY:0.934"] if page_number == 1 else [],
    )


def _result(page_number: int, source: TextSource) -> PageResult:
    return PageResult(
        page_number=page_number,
        width=612,
        height=792,
        blocks=[
            TextBlock(
                text="recognized sample text",
                bbox=(0.1, 0.1, 0.9, 0.2),
                block_type="text",
                confidence=96.0,
                source=source,
            )
        ],
        source=source,
        quality=QualityStatus.HIGH,
        metrics=PageMetrics(mean_ocr_confidence=96.0 if source is TextSource.TESSERACT else None),
    )


def test_quality_report_aggregates_page_checkpoints_without_text(tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="nld+eng")
    workspace = store.workspace(job.id)

    workspace.save_page_analysis(_analysis(1, PageDecision.USE_NATIVE))
    workspace.save_page_result(_result(1, TextSource.TESSERACT))
    workspace.save_page_analysis(_analysis(2, PageDecision.OCR_REQUIRED))
    workspace.save_page_result(_result(2, TextSource.TESSERACT))
    workspace.save_page_analysis(_analysis(3, PageDecision.EMPTY))

    path = build_quality_report(
        job,
        workspace,
        total_pages=3,
        generated_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    report = json.loads(path.read_text(encoding="utf-8"))

    assert path == workspace.output_dir / "quality-report.json"
    assert report["schema_version"] == 1
    assert report["report_type"] == "pdf_page_analysis_and_ocr_quality"
    assert report["job"]["total_pages"] == 3
    assert report["summary"]["decisions"] == {
        "use_native": 1,
        "ocr_required": 1,
        "empty": 1,
    }
    assert report["summary"]["pages_with_results"] == 2
    assert report["summary"]["result_sources"] == {"tesseract": 2}
    assert report["summary"]["result_qualities"] == {"high": 2}
    assert report["summary"]["mean_ocr_confidence"] == 96.0
    assert report["pages"][0]["native_ocr_similarity"] == 0.934
    assert report["pages"][2]["result"] is None
    assert report["privacy"]["recognized_text_included"] is False
    assert "recognized sample text" not in path.read_text(encoding="utf-8")


def test_quality_report_requires_every_analysis_checkpoint(tmp_path):
    source = tmp_path / "incomplete.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    workspace.save_page_analysis(_analysis(1, PageDecision.EMPTY))

    with pytest.raises(ValueError, match="page 2"):
        build_quality_report(job, workspace, total_pages=2)


def test_quality_report_rejects_naive_generated_timestamp(tmp_path):
    source = tmp_path / "timestamp.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    workspace.save_page_analysis(_analysis(1, PageDecision.EMPTY))

    with pytest.raises(ValueError, match="timezone-aware"):
        build_quality_report(
            job, workspace, total_pages=1, generated_at=datetime(2026, 11, 7)
        )
