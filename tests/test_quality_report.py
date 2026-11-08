from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conversion.document import DocumentChapter, DocumentModel, DocumentParagraph
from conversion.epub.builder import EpubMetadata

from conversion.models import PageMetrics, PageResult, QualityStatus, TextBlock, TextSource
from conversion.pdf.images import PageImage, PageImages
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
    assert report["schema_version"] == 3
    assert report["report_type"] == "pdf_page_analysis_and_ocr_quality"
    assert report["generator"]["name"] == "inkslim"
    assert report["generator"]["version"] == "development"
    assert report["generator"]["commit"] is None
    assert report["job"]["id"] == job.id
    assert report["job"]["total_pages"] == 3
    assert report["job"]["language_selection"] == "nld+eng"
    assert report["job"]["ocr_languages"] == ["nld", "eng"]
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


def test_quality_report_includes_epub_statistics_and_content_warnings(tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-placeholder")
    output = tmp_path / "book.epub"
    output.write_bytes(b"epub-output")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="nld")
    workspace = store.workspace(job.id)

    workspace.save_page_analysis(_analysis(1, PageDecision.USE_NATIVE))
    workspace.save_page_result(_result(1, TextSource.NATIVE))
    workspace.save_page_cleaned_paragraphs(1, [
        {"paragraph_index": 0, "text": "Dit is een eerste alinea."},
        {"paragraph_index": 1, "text": "Een tweede alinea."},
    ])
    workspace.save_page_analysis(_analysis(2, PageDecision.OCR_REQUIRED))
    workspace.save_page_result(_result(2, TextSource.TESSERACT))
    workspace.save_page_cleaned_paragraphs(2, [])
    workspace.save_page_analysis(_analysis(3, PageDecision.EMPTY))
    workspace.save_page_cleaned_paragraphs(3, [])
    workspace.save_document_model(DocumentModel(
        metadata=EpubMetadata(title="book", language="nl"),
        chapters=[DocumentChapter("Hoofdstuk 1", [
            DocumentParagraph("Dit is een eerste alinea."),
            DocumentParagraph("Een tweede alinea.", kind="h2"),
        ])],
    ))

    path = build_quality_report(
        job, workspace, total_pages=3, output_path=output,
        generated_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    report = json.loads(path.read_text(encoding="utf-8"))

    assert report["output"] == {
        "format": "epub",
        "source_pdf_bytes": source.stat().st_size,
        "epub_bytes": output.stat().st_size,
        "chapter_count": 1,
        "paragraph_count": 2,
        "heading_count": 1,
        "word_count": 8,
    }
    assert report["content_quality"]["pages_with_text"] == 1
    assert report["content_quality"]["pages_without_text"] == 1
    assert report["content_quality"]["empty_source_pages"] == 1
    assert report["content_quality"]["eligible_source_pages"] == 2
    assert report["content_quality"]["text_page_coverage"] == 0.5
    assert {item["code"] for item in report["content_quality"]["warnings"]} == {
        "OCR_PAGES_WITHOUT_TEXT", "PAGES_WITHOUT_TEXT"
    }
    assert report["summary"]["warning_count"] == 3
    assert report["privacy"]["recognized_text_included"] is False


def test_quality_report_warns_when_no_body_text_was_reconstructed(tmp_path):
    source = tmp_path / "empty.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    workspace.save_page_analysis(_analysis(1, PageDecision.EMPTY))
    workspace.save_page_cleaned_paragraphs(1, [])
    workspace.save_document_model(DocumentModel(
        metadata=EpubMetadata(title="empty", language="en"),
        chapters=[DocumentChapter("empty", [
            DocumentParagraph("[Empty document]")
        ])],
    ))

    report = json.loads(build_quality_report(
        job, workspace, total_pages=1
    ).read_text(encoding="utf-8"))
    assert report["output"]["word_count"] == 0
    assert report["content_quality"]["warnings"][0]["code"] == "NO_BODY_TEXT_EXTRACTED"


def test_quality_report_summarizes_extracted_images_and_incomplete_extraction(tmp_path):
    source = tmp_path / "illustrated.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)

    for page_number in range(1, 4):
        workspace.save_page_analysis(_analysis(page_number, PageDecision.USE_NATIVE))

    first_asset = workspace.root / "images/000001-0001.png"
    second_asset = workspace.root / "images/000001-0002.jpg"
    first_asset.parent.mkdir(parents=True, exist_ok=True)
    first_asset.write_bytes(b"png-image-data")
    second_asset.write_bytes(b"jpeg-image-data")
    page_images = (
        PageImage(1, 1, (0.1, 0.1, 0.5, 0.5), 100, 100,
                  "image/png", "images/000001-0001.png", 0.16),
        PageImage(1, 2, (0.5, 0.1, 0.9, 0.5), 200, 100,
                  "image/jpeg", "images/000001-0002.jpg", 0.16),
    )
    workspace.save_page_images(PageImages(1, page_images, detected_count=2))
    workspace.save_page_images(PageImages(
        2, (), ("IMAGE_EXTRACTION_FAILED:1",), detected_count=1
    ))
    workspace.save_page_images(PageImages(3, (), detected_count=0))

    report_path = build_quality_report(job, workspace, total_pages=3)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    images = report["images"]

    assert images["status"] == "not_yet_embedded"
    assert images["pages_scanned"] == 3
    assert images["pages_with_images"] == 2
    assert images["detected_count"] == 3
    assert images["detection_complete"] is True
    assert images["extracted_count"] == 2
    assert images["not_extracted_count"] == 1
    assert images["missing_asset_count"] == 0
    assert images["extracted_bytes"] == len(b"png-image-data") + len(b"jpeg-image-data")
    assert images["embedded_in_epub"] is None
    assert images["warnings"] == [{
        "code": "IMAGE_EXTRACTION_FAILED",
        "affected_page_count": 1,
        "page_numbers": [2],
    }]
    assert "images/000001-0001.png" not in report_path.read_text(encoding="utf-8")
    assert report["pages"][0]["images"]["detected_count"] == 2


def test_quality_report_flags_missing_image_checkpoints(tmp_path):
    source = tmp_path / "partial.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    workspace.save_page_analysis(_analysis(1, PageDecision.EMPTY))

    report = json.loads(build_quality_report(
        job, workspace, total_pages=1
    ).read_text(encoding="utf-8"))
    assert report["images"]["detection_complete"] is False
    assert report["images"]["missing_checkpoint_pages"] == [1]
