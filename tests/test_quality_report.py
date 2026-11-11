from __future__ import annotations

import json
import zipfile
from io import BytesIO

from PIL import Image
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
        character_count=0 if decision is PageDecision.EMPTY else 120,
        word_count=0 if decision is PageDecision.EMPTY else 22,
        image_count=0 if decision is PageDecision.EMPTY else 1,
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
    workspace.save_page_cleaned_paragraphs(1, [{"text": "recognized sample text"}])
    workspace.save_page_analysis(_analysis(2, PageDecision.OCR_REQUIRED))
    workspace.save_page_result(_result(2, TextSource.TESSERACT))
    workspace.save_page_analysis(_analysis(3, PageDecision.EMPTY))
    workspace.save_document_language_analysis({
        "schema_version": 1,
        "method": "weighted_reconstructed_page_language",
        "language": "en",
        "detected_candidate": "en",
        "evaluated_pages": 31,
        "supporting_pages": 29,
        "vote_share": 0.9842,
    })

    path = build_quality_report(
        job,
        workspace,
        total_pages=3,
        generated_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    report = json.loads(path.read_text(encoding="utf-8"))

    assert path == workspace.output_dir / "quality-report.json"
    assert report["schema_version"] == 9
    assert report["report_type"] == "pdf_page_analysis_and_ocr_quality"
    assert report["generator"]["name"] == "inkslim"
    assert report["generator"]["version"] == "0.1.0-dev"
    assert report["generator"]["commit"] is None
    assert report["job"]["id"] == job.id
    assert report["job"]["total_pages"] == 3
    assert report["job"]["language_selection"] == "nld+eng"
    assert report["job"]["ocr_languages"] == ["nld", "eng"]
    assert report["document_language"]["language"] == "en"
    assert report["document_language"]["supporting_pages"] == 29
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
    assert report["pages"][2]["content_classification"]["type"] == "unknown"
    assert report["summary"]["page_content_types"] == {"mixed": 2, "unknown": 1}
    assert report["text_fidelity"]["status"] == "available"
    assert report["text_fidelity"]["pages_compared"] == 2
    assert report["pages"][0]["text_fidelity"]["source_token_count"] == 3
    assert report["pages"][0]["text_fidelity"]["reconstructed_token_count"] == 3
    assert report["pages"][0]["text_fidelity"]["token_retention"] == 1.0
    assert report["privacy"]["recognized_text_included"] is False
    assert "recognized sample text" not in path.read_text(encoding="utf-8")



def test_quality_report_flags_document_structure_candidates_without_exposing_text(tmp_path):
    source = tmp_path / "structure.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)

    for page_number in range(1, 4):
        workspace.save_page_analysis(_analysis(page_number, PageDecision.USE_NATIVE))
        workspace.save_page_cleaned_paragraphs(page_number, [{"text": f"Body text on page {page_number}."}])

    workspace.save_document_model(DocumentModel(
        metadata=EpubMetadata(title="Structure", language="en"),
        chapters=[
            DocumentChapter("Chapter One", [
                DocumentParagraph("CHAPTER ONE", kind="h2", source_page=1),
                DocumentParagraph("Body text on page one.", source_page=1),
                DocumentParagraph("1", source_page=1),
            ]),
            DocumentChapter("Chapter One", [
                DocumentParagraph("Chapter One", kind="h2", source_page=2),
                DocumentParagraph("Body text on page two.", source_page=2),
                DocumentParagraph("2", source_page=2),
                DocumentParagraph("Out of order paragraph.", source_page=1),
            ]),
        ],
    ))

    path = build_quality_report(job, workspace, total_pages=3)
    report_text = path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    diagnostics = report["document_structure"]

    assert diagnostics["status"] == "available"
    assert diagnostics["reading_order"]["status"] == "review"
    assert diagnostics["reading_order"]["out_of_order_transitions"] == 1
    assert diagnostics["duplicate_headings"]["count"] == 1
    assert diagnostics["duplicate_headings"]["occurrences"][0] == {
        "occurrence_count": 2,
        "page_numbers": [1, 2],
    }
    assert diagnostics["possible_page_number_artifacts"] == {
        "count": 2,
        "page_numbers": [1, 2],
    }
    assert {
        warning["code"] for warning in diagnostics["warnings"]
    } == {
        "SOURCE_PAGE_ORDER_REGRESSION",
        "POSSIBLE_DUPLICATE_HEADINGS",
        "POSSIBLE_PAGE_NUMBER_ARTIFACTS",
    }
    assert "CHAPTER ONE" not in report_text
    assert "Out of order paragraph" not in report_text
    assert report["summary"]["warning_count"] >= 3


def test_quality_report_marks_document_structure_unavailable_without_model(tmp_path):
    source = tmp_path / "no-model.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    workspace.save_page_analysis(_analysis(1, PageDecision.EMPTY))

    report = json.loads(build_quality_report(job, workspace, total_pages=1).read_text())
    assert report["document_structure"]["status"] == "unavailable"
    assert report["document_structure"]["warnings"] == []



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



def test_quality_report_counts_images_embedded_in_epub(tmp_path):
    source = tmp_path / "illustrated.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    workspace.save_page_analysis(_analysis(1, PageDecision.USE_NATIVE))
    workspace.save_page_images(PageImages(1, (), detected_count=0))

    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as archive:
        archive.writestr("OEBPS/images/example.png", b"1234567890")

    report_path = build_quality_report(
        job, workspace, total_pages=1, output_path=epub,
    )
    images = json.loads(report_path.read_text(encoding="utf-8"))["images"]
    assert images["status"] == "embedded"
    assert images["embedded_in_epub"] == 1
    assert images["embedded_bytes"] == 10

def test_quality_report_records_epub_image_optimization_savings(tmp_path):
    source = tmp_path / "optimized-images.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    workspace.save_page_analysis(_analysis(1, PageDecision.USE_NATIVE))

    asset = workspace.page_image_asset_prefix(1, 1).with_suffix(".jpg")
    asset.parent.mkdir(parents=True, exist_ok=True)
    Image.effect_noise((600, 800), 90).convert("RGB").save(
        asset, "JPEG", quality=100, subsampling=0
    )
    original_size = asset.stat().st_size
    relative = asset.relative_to(workspace.root).as_posix()
    workspace.save_page_images(PageImages(
        1,
        (PageImage(
            1, 1, (0.1, 0.1, 0.9, 0.9), 600, 800,
            "image/jpeg", relative, 0.64,
        ),),
        detected_count=1,
    ))

    optimized = BytesIO()
    with Image.open(asset) as original_image:
        original_image.save(
            optimized, "JPEG", quality=85, optimize=True, progressive=True
        )
    optimized_bytes = optimized.getvalue()
    epub = tmp_path / "optimized.epub"
    with zipfile.ZipFile(epub, "w") as archive:
        archive.writestr("OEBPS/" + relative, optimized_bytes)

    report_path = build_quality_report(
        job, workspace, total_pages=1, output_path=epub,
    )
    images = json.loads(report_path.read_text(encoding="utf-8"))["images"]
    assert images["optimized_image_count"] == 1
    assert images["optimization_saved_bytes"] == original_size - len(optimized_bytes)
    assert images["optimization_method"] == (
        "jpeg_quality_85_optimize_progressive_png_lossless_optimize"
    )


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


def test_quality_report_detects_only_byte_identical_cover_images_without_removing_them(tmp_path):
    source = tmp_path / "duplicate-cover.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)

    first_asset = workspace.root / "images/000001-0001.png"
    duplicate_asset = workspace.root / "images/000002-0001.png"
    non_duplicate_asset = workspace.root / "images/000003-0001.png"
    first_asset.parent.mkdir(parents=True, exist_ok=True)
    cover_image = Image.new("L", (96, 96))
    cover_image.putdata([x * 255 // 95 for y in range(96) for x in range(96)])
    cover_buffer = BytesIO()
    cover_image.save(cover_buffer, format="PNG")
    cover_bytes = cover_buffer.getvalue()
    first_asset.write_bytes(cover_bytes)
    duplicate_asset.write_bytes(cover_bytes)
    other_image = Image.new("L", (96, 96))
    other_image.putdata([255 - (x * 255 // 95) for y in range(96) for x in range(96)])
    other_image.save(non_duplicate_asset, format="PNG")
    resized_image = cover_image.resize((48, 48), Image.Resampling.LANCZOS)
    resized_image.save(workspace.root / "images/000004-0001.png", format="PNG")

    cover = PageImage(
        1, 1, (0.0, 0.0, 1.0, 1.0), 100, 100,
        "image/png", "images/000001-0001.png", 1.0,
    )
    duplicate = PageImage(
        2, 1, (0.1, 0.1, 0.9, 0.9), 100, 100,
        "image/png", "images/000002-0001.png", 0.64,
    )
    workspace.save_page_images(PageImages(1, (cover,), detected_count=1))
    workspace.save_page_images(PageImages(2, (duplicate,), detected_count=1))
    workspace.save_page_images(PageImages(3, (
        PageImage(
            3, 1, (0.1, 0.1, 0.9, 0.9), 100, 100,
            "image/png", "images/000003-0001.png", 0.64,
        ),
    ), detected_count=1))
    workspace.save_page_analysis(PageAnalysis(
        page_number=1, width=612, height=792, character_count=0,
        word_count=0, image_count=1, has_native_text=False,
        native_text_quality=None, decision=PageDecision.EMPTY,
    ))
    workspace.save_page_analysis(PageAnalysis(
        page_number=2, width=612, height=792, character_count=0,
        word_count=0, image_count=1, has_native_text=False,
        native_text_quality=None, decision=PageDecision.EMPTY,
    ))
    workspace.save_page_analysis(PageAnalysis(
        page_number=3, width=612, height=792, character_count=0,
        word_count=0, image_count=1, has_native_text=False,
        native_text_quality=None, decision=PageDecision.EMPTY,
    ))
    workspace.save_page_images(PageImages(4, (
        PageImage(4, 1, (0.1, 0.1, 0.9, 0.9), 48, 48,
                  "image/png", "images/000004-0001.png", 0.64),
    ), detected_count=1))
    workspace.save_page_analysis(PageAnalysis(
        page_number=4, width=612, height=792, character_count=0,
        word_count=0, image_count=1, has_native_text=False,
        native_text_quality=None, decision=PageDecision.EMPTY,
    ))

    report_path = build_quality_report(job, workspace, total_pages=4)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    duplicate_report = report["images"]["duplicate_cover_detection"]

    assert report["schema_version"] == 8
    assert duplicate_report == {
        "status": "complete",
        "method": "sha256_exact_file_bytes_and_dhash64",
        "cover_candidate": {
            "page_number": 1,
            "image_index": 1,
            "coverage": 1.0,
            "classification_confidence": 0.78,
        },
        "duplicate_count": 1,
        "matches": [{"page_number": 2, "image_index": 1}],
        "near_duplicate_count": 1,
        "near_matches": [{"page_number": 4, "image_index": 1, "hamming_distance": 0}],
        "perceptual_hamming_threshold": 5,
        "removed_count": 0,
        "automatic_removal_enabled": False,
        "warnings": [],
    }
    assert first_asset.read_bytes() == cover_bytes
    assert duplicate_asset.read_bytes() == cover_bytes
    assert non_duplicate_asset.read_bytes() != cover_bytes
    assert (workspace.root / "images/000004-0001.png").is_file()


def test_duplicate_cover_detection_is_incomplete_when_first_page_image_checkpoint_is_partial(tmp_path):
    source = tmp_path / "partial-cover.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)

    cover_path = workspace.root / "images/000001-0001.png"
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    cover_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"cover bytes")
    workspace.save_page_images(PageImages(1, (
        PageImage(
            1, 1, (0.0, 0.0, 1.0, 1.0), 100, 100,
            "image/png", "images/000001-0001.png", 1.0,
        ),
    ), detected_count=2))
    workspace.save_page_analysis(PageAnalysis(
        page_number=1, width=612, height=792, character_count=0,
        word_count=0, image_count=1, has_native_text=False,
        native_text_quality=None, decision=PageDecision.EMPTY,
    ))
    workspace.save_page_images(PageImages(2, (), detected_count=0))
    workspace.save_page_analysis(PageAnalysis(
        page_number=2, width=612, height=792, character_count=0,
        word_count=0, image_count=0, has_native_text=False,
        native_text_quality=None, decision=PageDecision.EMPTY,
    ))

    report = json.loads(build_quality_report(
        job, workspace, total_pages=2
    ).read_text(encoding="utf-8"))
    duplicate_report = report["images"]["duplicate_cover_detection"]
    assert duplicate_report["status"] == "incomplete"
    assert duplicate_report["duplicate_count"] == 0
    assert duplicate_report["removed_count"] == 0



def test_duplicate_cover_detection_ignores_small_images_and_rejects_unsafe_paths(tmp_path):
    source = tmp_path / "unsafe-cover.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    image_path = workspace.root / "images/cover.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", (96, 96))
    image.putdata([x * 255 // 95 for y in range(96) for x in range(96)])
    image.save(image_path, format="PNG")
    cover = PageImage(1, 1, (0, 0, 1, 1), 96, 96, "image/png", "images/cover.png", 1.0)
    small_logo_path = workspace.root / "images/small-logo.png"
    Image.open(image_path).resize((48, 48), Image.Resampling.LANCZOS).save(small_logo_path)
    logo = PageImage(2, 1, (0, 0, 0.3, 0.3), 48, 48, "image/png", "images/small-logo.png", 0.09)
    workspace.save_page_images(PageImages(1, (cover,), detected_count=1))
    workspace.save_page_images(PageImages(2, (logo,), detected_count=1))
    for number in (1, 2):
        workspace.save_page_analysis(PageAnalysis(
            page_number=number, width=612, height=792, character_count=0,
            word_count=0, image_count=1, has_native_text=False,
            native_text_quality=None, decision=PageDecision.EMPTY,
        ))

    report = json.loads(build_quality_report(job, workspace, total_pages=2).read_text())
    duplicates = report["duplicate_covers"]
    assert duplicates["near_duplicate_count"] == 0
    assert duplicates["duplicate_count"] == 0
    assert duplicates["status"] == "complete"

    with pytest.raises(ValueError, match="safe relative images path"):
        PageImage(3, 1, (0, 0, 1, 1), 96, 96, "image/png", "../../outside.png", 1.0)


def test_corrupt_cover_image_marks_diagnostics_incomplete_without_failing_report(tmp_path):
    source = tmp_path / "corrupt-cover.pdf"
    source.write_bytes(b"%PDF-placeholder")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    workspace = store.workspace(job.id)
    asset = workspace.root / "images/corrupt.png"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"not a real PNG")
    workspace.save_page_images(PageImages(1, (
        PageImage(1, 1, (0, 0, 1, 1), 100, 100, "image/png", "images/corrupt.png", 1.0),
    ), detected_count=1))
    workspace.save_page_analysis(PageAnalysis(
        page_number=1, width=612, height=792, character_count=0,
        word_count=0, image_count=1, has_native_text=False,
        native_text_quality=None, decision=PageDecision.EMPTY,
    ))

    report = json.loads(build_quality_report(job, workspace, total_pages=1).read_text())
    assert report["duplicate_covers"]["status"] == "incomplete"
    assert "COVER_IMAGE_UNREADABLE_OR_INVALID" in report["duplicate_covers"]["warnings"]
