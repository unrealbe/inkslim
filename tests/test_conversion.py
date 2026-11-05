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
from conversion.pdf.analyzer import PdfAnalysisError, analyze_pdf
from conversion.pdf.models import PageAnalysis, PageDecision
from conversion.pdf.quality import assess_native_text
from conversion.pdf.renderer import (
    PDFRenderLimits,
    RenderPlanError,
    build_render_plan,
    render_pdf_page,
)


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
    assert workspace.analysis_dir.is_dir()
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
    assert config.pdf_render_dpi == 300
    assert config.min_render_dpi == 120
    assert config.max_render_pixels == 50_000_000
    assert config.max_render_dimension == 12_000

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


# ============================================================================
# Checkpoint compatibility and PDF page analysis
# ============================================================================


def make_image_pdf(path: Path, pages: list[str]) -> Path:
    """Create a small PDF fixture; page kinds are text, image, mixed, blank, number."""
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    image_path = path.with_suffix(".png")
    image = Image.new("RGB", (500, 700), "white")
    draw = ImageDraw.Draw(image)
    draw.text((35, 35), "A scanned page with readable printed words.", fill="black")
    image.save(image_path)

    pdf_canvas = canvas.Canvas(str(path), pagesize=letter)
    for kind in pages:
        if kind == "text":
            pdf_canvas.drawString(
                72, 720,
                "A sufficiently long native text paragraph that should be kept without OCR.",
            )
        elif kind == "image":
            pdf_canvas.drawImage(str(image_path), 72, 250, width=400, height=500)
        elif kind == "mixed":
            pdf_canvas.drawImage(str(image_path), 72, 250, width=400, height=500)
            # Simulate an invisible/embedded OCR text layer over a scan.
            pdf_canvas.drawString(
                72, 720,
                "A sufficiently long native OCR layer with meaningful words and spacing.",
            )
        elif kind == "number":
            pdf_canvas.drawString(300, 30, "127")
        elif kind != "blank":
            raise ValueError(f"Unknown fixture page kind: {kind}")
        pdf_canvas.showPage()
    pdf_canvas.save()
    image_path.unlink(missing_ok=True)
    return path


def test_legacy_page_result_checkpoint_is_migrated_and_saved(store, tmp_path):
    job = make_job(store, tmp_path)
    workspace = store.workspace(job.id)
    legacy = make_page(1).to_dict()
    legacy.pop("schema_version")
    legacy.pop("metrics")
    legacy.pop("warnings")
    path = workspace.page_result_path(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    restored = workspace.load_page_result(1)

    assert restored is not None
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 1
    assert "metrics" in migrated and "warnings" in migrated


def test_unknown_checkpoint_version_is_quarantined_and_reprocessed(store, tmp_path, caplog):
    job = make_job(store, tmp_path)
    workspace = store.workspace(job.id)
    value = make_page(1).to_dict()
    value["schema_version"] = 999
    path = workspace.page_result_path(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

    assert workspace.load_page_result(1) is None
    assert workspace.first_missing_page(1) == 1
    quarantined = list((workspace.pages_dir / "quarantine").glob("*.unknown-version.*.json"))
    assert len(quarantined) == 1
    assert "quarantined" in caplog.text.lower()


def test_corrupt_checkpoint_is_quarantined(store, tmp_path, caplog):
    job = make_job(store, tmp_path)
    workspace = store.workspace(job.id)
    path = workspace.page_result_path(4)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid JSON", encoding="utf-8")

    assert workspace.load_page_result(4) is None
    quarantined = list((workspace.pages_dir / "quarantine").glob("*.corrupt.*.json"))
    assert len(quarantined) == 1
    assert "corrupt" in caplog.text.lower()


def test_valid_checkpoint_is_loaded_and_not_marked_for_reprocessing(store, tmp_path):
    job = make_job(store, tmp_path)
    workspace = store.workspace(job.id)
    workspace.save_page_result(make_page(1))
    workspace.save_page_result(make_page(3))

    assert workspace.load_page_result(1) == make_page(1)
    assert workspace.first_missing_page(3) == 2
    assert workspace.completed_page_count(3) == 2


def test_page_analysis_checkpoint_round_trip_and_resume(store, tmp_path):
    job = make_job(store, tmp_path)
    workspace = store.workspace(job.id)
    analysis = PageAnalysis(
        page_number=1,
        width=612,
        height=792,
        character_count=1800,
        word_count=320,
        image_count=0,
        has_native_text=True,
        native_text_quality=0.91,
        decision=PageDecision.USE_NATIVE,
        meaningful_character_ratio=0.96,
        control_character_ratio=0.0,
        replacement_character_ratio=0.0,
        single_character_word_ratio=0.03,
        geometry_usable_ratio=0.99,
    )

    workspace.save_page_analysis(analysis)

    assert workspace.load_page_analysis(1) == analysis
    assert workspace.first_missing_analysis(3) == 2


def test_pdf_analyzer_classifies_pages_individually(tmp_path):
    pdf_path = make_image_pdf(
        tmp_path / "mixed.pdf",
        ["text", "image", "mixed", "blank", "number"],
    )

    pages = analyze_pdf(pdf_path)

    assert len(pages) == 5
    assert pages[0].decision is PageDecision.USE_NATIVE
    assert pages[0].native_text_quality is not None
    assert pages[0].native_text_quality > 0.7
    assert pages[0].image_count == 0

    assert pages[1].decision is PageDecision.OCR_REQUIRED
    assert pages[1].image_count == 1
    assert pages[1].render_dpi == 300
    assert pages[1].render_width_px > 0

    # The embedded OCR layer is reused when its text and geometry are plausible.
    assert pages[2].decision is PageDecision.USE_NATIVE
    assert pages[2].image_count == 1

    assert pages[3].decision is PageDecision.EMPTY
    # A footer-only page number does not trigger OCR.
    assert pages[4].decision is PageDecision.EMPTY


def test_native_text_quality_flags_suspicious_ocr_layer():
    clean = assess_native_text(
        "The quick brown fox jumps over the lazy dog. This is normal book text.",
        image_count=0,
        geometry_usable_ratio=0.98,
    )
    assert clean.decision is PageDecision.USE_NATIVE

    corrupted = assess_native_text(
        ("A\ufffd\x01" * 30) + " short words",
        image_count=1,
        geometry_usable_ratio=0.95,
    )
    assert corrupted.decision is PageDecision.OCR_REQUIRED
    assert "HIGH_REPLACEMENT_CHARACTER_RATIO" in corrupted.warnings
    assert "HIGH_CONTROL_CHARACTER_RATIO" in corrupted.warnings


def test_short_or_geometrically_unusable_text_requires_ocr_or_is_empty():
    page_number = assess_native_text(
        "127", image_count=0, geometry_usable_ratio=1.0
    )
    assert page_number.decision is PageDecision.EMPTY

    text_over_scan = assess_native_text(
        "127", image_count=1, geometry_usable_ratio=1.0
    )
    assert text_over_scan.decision is PageDecision.OCR_REQUIRED

    bad_geometry = assess_native_text(
        "This is a sufficiently long text layer, but its glyph positions are implausible.",
        image_count=0,
        geometry_usable_ratio=0.1,
    )
    assert bad_geometry.decision is PageDecision.OCR_REQUIRED
    assert "POOR_TEXT_GEOMETRY" in bad_geometry.warnings


def test_render_plan_reduces_dpi_to_respect_limits():
    plan = build_render_plan(
        4000,
        4000,
        limits=PDFRenderLimits(
            dpi=300,
            minimum_dpi=120,
            max_pixels=50_000_000,
            max_dimension=12_000,
        ),
    )

    assert plan.actual_dpi < 300
    assert plan.actual_dpi >= 120
    assert plan.width_px * plan.height_px <= 50_000_000
    assert max(plan.width_px, plan.height_px) <= 12_000
    assert plan.warnings


def test_render_plan_rejects_pages_that_exceed_limits_at_minimum_dpi():
    with pytest.raises(RenderPlanError):
        build_render_plan(
            50_000,
            50_000,
            limits=PDFRenderLimits(
                dpi=300,
                minimum_dpi=120,
                max_pixels=50_000_000,
                max_dimension=12_000,
            ),
        )


def test_analyzer_marks_page_that_cannot_be_rendered_safely(tmp_path):
    from PIL import Image
    from reportlab.pdfgen import canvas

    image_path = tmp_path / "pixel.png"
    Image.new("RGB", (10, 10), "white").save(image_path)
    pdf_path = tmp_path / "oversized-page.pdf"
    pdf_canvas = canvas.Canvas(str(pdf_path), pagesize=(50_000, 50_000))
    pdf_canvas.drawImage(str(image_path), 0, 0, width=50_000, height=50_000)
    pdf_canvas.showPage()
    pdf_canvas.save()

    pages = analyze_pdf(
        pdf_path,
        render_limits=PDFRenderLimits(
            dpi=300,
            minimum_dpi=120,
            max_pixels=50_000_000,
            max_dimension=12_000,
        ),
    )

    assert pages[0].decision is PageDecision.OCR_REQUIRED
    assert pages[0].render_dpi is None
    assert "RENDER_LIMIT_EXCEEDED" in pages[0].warnings


def test_renderer_writes_a_bounded_page_image(tmp_path):
    from PIL import Image
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    import pypdfium2 as pdfium

    pdf_path = tmp_path / "render.pdf"
    pdf_canvas = canvas.Canvas(str(pdf_path), pagesize=letter)
    pdf_canvas.drawString(72, 720, "A page to render.")
    pdf_canvas.showPage()
    pdf_canvas.save()

    destination = tmp_path / "rendered" / "000001.png"
    limits = PDFRenderLimits(dpi=150, minimum_dpi=120)
    with pdfium.PdfDocument(str(pdf_path)) as document:
        page = document[0]
        try:
            plan = render_pdf_page(page, destination, limits=limits)
        finally:
            page.close()

    assert destination.is_file()
    assert plan.actual_dpi == 150
    with Image.open(destination) as rendered:
        width, height = rendered.size
    assert width <= limits.max_dimension
    assert height <= limits.max_dimension
    assert width * height <= limits.max_pixels


def test_analyzer_reports_password_protected_and_corrupt_pdfs(tmp_path):
    from reportlab.pdfgen import canvas

    encrypted_path = tmp_path / "encrypted.pdf"
    encrypted = canvas.Canvas(str(encrypted_path), encrypt="test-password")
    encrypted.drawString(72, 720, "Password protected book text")
    encrypted.showPage()
    encrypted.save()

    with pytest.raises(PdfAnalysisError) as encrypted_error:
        analyze_pdf(encrypted_path)
    assert encrypted_error.value.code == "PASSWORD_PROTECTED"

    corrupt_path = tmp_path / "corrupt.pdf"
    corrupt_path.write_bytes(b"This is not a PDF file")
    with pytest.raises(PdfAnalysisError) as corrupt_error:
        analyze_pdf(corrupt_path)
    assert corrupt_error.value.code == "INVALID_PDF"


def test_pdf_analysis_checkpoints_are_saved_and_reused_on_restart(
    store, tmp_path, monkeypatch
):
    from conversion.pdf import analyzer as analyzer_module

    pdf_path = make_image_pdf(tmp_path / "checkpointed.pdf", ["text", "image", "blank"])
    job = store.create_job(pdf_path, pdf_path.name, language="nld+eng")
    workspace = store.workspace(job.id)

    first_run = analyze_pdf(Path(job.input_path), workspace=workspace)

    assert len(first_run) == 3
    assert workspace.first_missing_analysis(3) == 4
    assert all(workspace.analysis_result_path(page).is_file() for page in range(1, 4))

    def unexpected_reanalysis(_page):
        raise AssertionError("A valid per-page checkpoint should be reused")

    monkeypatch.setattr(analyzer_module, "extract_native_text", unexpected_reanalysis)
    resumed = analyze_pdf(Path(job.input_path), workspace=workspace)

    assert resumed == first_run


def test_unknown_page_analysis_checkpoint_is_quarantined(store, tmp_path):
    job = make_job(store, tmp_path)
    workspace = store.workspace(job.id)
    analysis = PageAnalysis(
        page_number=1,
        width=612,
        height=792,
        character_count=100,
        word_count=20,
        image_count=0,
        has_native_text=True,
        native_text_quality=0.9,
        decision=PageDecision.USE_NATIVE,
    )
    path = workspace.save_page_analysis(analysis)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 500
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert workspace.load_page_analysis(1) is None
    quarantined = list(
        (workspace.analysis_dir / "quarantine").glob("*.unknown-version.*.json")
    )
    assert len(quarantined) == 1
