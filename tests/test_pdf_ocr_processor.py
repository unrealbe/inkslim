from __future__ import annotations

from pathlib import Path

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from conversion.models import JobStatus, PageMetrics, PageResult, QualityStatus, TextBlock, TextSource
from conversion.ocr import OcrError
from conversion.pdf.ocr_processor import PdfConversionJobProcessor
from conversion.storage import JobStore
from conversion.worker import ConversionWorker


class FakeOcrEngine:
    def __init__(self):
        self.calls = []

    def recognize(self, image_path, languages, *, page_number=1):
        self.calls.append((Path(image_path), languages, page_number))
        return PageResult(
            page_number=page_number, width=200, height=300,
            blocks=[TextBlock(
                text="Dit is herkende tekst", bbox=(0.1, 0.1, 0.9, 0.2),
                block_type="text", confidence=96.0, source=TextSource.TESSERACT,
                line_number=1, paragraph_number=1,
            )],
            source=TextSource.TESSERACT, quality=QualityStatus.HIGH,
            metrics=PageMetrics(mean_ocr_confidence=96.0),
        )


def make_scanned_pdf(path: Path, page_count: int = 1) -> Path:
    image_path = path.with_suffix(".png")
    Image.new("RGB", (200, 300), "white").save(image_path)
    pdf = canvas.Canvas(str(path), pagesize=(200, 300))
    for _ in range(page_count):
        pdf.drawImage(ImageReader(str(image_path)), 0, 0, width=200, height=300)
        pdf.showPage()
    pdf.save()
    return path


def test_worker_runs_ocr_after_analysis_and_saves_page_result(tmp_path):
    source = make_scanned_pdf(tmp_path / "scan.pdf")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="nld+eng")
    engine = FakeOcrEngine()
    worker = ConversionWorker(store, PdfConversionJobProcessor(engine=engine), worker_id="ocr-worker")

    assert worker.run_once()
    assert store.get_job(job.id).status is JobStatus.READY_FOR_OCR
    assert worker.run_once()

    result = store.get_job(job.id)
    checkpoint = store.workspace(job.id).load_page_result(1)
    assert result.status is JobStatus.EXTRACTING
    assert checkpoint is not None
    assert checkpoint.blocks[0].text == "Dit is herkende tekst"
    assert engine.calls[0][1:] == (["nld", "eng"], 1)
    assert engine.calls[0][0].is_file()
    paragraphs = store.workspace(job.id).load_page_paragraphs(1)
    assert paragraphs is not None
    assert [item["text"] for item in paragraphs] == ["Dit is herkende tekst"]
    dehyphenated = store.workspace(job.id).load_page_dehyphenated(1)
    assert dehyphenated == ([{"paragraph_index": 1, "text": "Dit is herkende tekst", "bbox": [0.1, 0.1, 0.9, 0.2], "confidence": 96.0, "source": "tesseract", "line_indices": [1]}], 0)

    marginalia = store.workspace(job.id).load_marginalia_analysis()
    cleaned = store.workspace(job.id).load_page_cleaned_paragraphs(1)
    assert marginalia is not None
    assert marginalia["removed_paragraphs"] == 0
    assert cleaned == dehyphenated[0]

    structure = store.workspace(job.id).load_structure_analysis()
    page_structure = store.workspace(job.id).load_page_structure(1)
    assert structure is not None
    assert structure["chapter_count"] == 0
    assert page_structure == []


def test_ocr_phase_skips_valid_page_result_checkpoint(tmp_path):
    source = make_scanned_pdf(tmp_path / "resume-scan.pdf")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    worker = ConversionWorker(store, PdfConversionJobProcessor(engine=FakeOcrEngine()), worker_id="analysis")
    worker.run_once()

    workspace = store.workspace(job.id)
    checkpoint_engine = FakeOcrEngine()
    checkpoint = checkpoint_engine.recognize(
        tmp_path / "not-read.png", ["nld"], page_number=1
    )
    workspace.save_page_result(checkpoint)

    # Use a fresh engine to verify that the worker does not invoke OCR again.
    engine = FakeOcrEngine()
    second_worker = ConversionWorker(
        store, PdfConversionJobProcessor(engine=engine), worker_id="resume"
    )
    second_worker.run_once()

    assert engine.calls == []
    assert store.get_job(job.id).status is JobStatus.EXTRACTING
    paragraphs = store.workspace(job.id).load_page_paragraphs(1)
    assert paragraphs is not None
    assert [item["text"] for item in paragraphs] == ["Dit is herkende tekst"]


def test_interrupted_ocr_job_recovers_to_ready_for_ocr(tmp_path):
    source = make_scanned_pdf(tmp_path / "interrupted.pdf")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)
    store.claim_next_job("analysis")
    store.mark_ready_for_ocr(job.id)
    claimed = store.claim_next_job("ocr-worker")
    assert claimed is not None and claimed.status is JobStatus.OCR

    assert store.recover_interrupted_jobs() == 1
    recovered = store.get_job(job.id)
    assert recovered.status is JobStatus.READY_FOR_OCR
    assert recovered.worker_id is None


def test_ocr_failure_is_reported_with_safe_error(tmp_path):
    source = make_scanned_pdf(tmp_path / "failure.pdf")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name)

    class BrokenEngine:
        def recognize(self, *_args, **_kwargs):
            raise OcrError("OCR_TIMEOUT", "Text recognition exceeded the page time limit.")

    worker = ConversionWorker(store, PdfConversionJobProcessor(engine=BrokenEngine()), worker_id="failure")
    worker.run_once()
    worker.run_once()

    failed = store.get_job(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "OCR_TIMEOUT"
    assert failed.error_message == "Text recognition exceeded the page time limit."


def test_structure_annotations_recovered_when_interrupted_after_global_analysis(tmp_path):
    class ChapterOcrEngine:
        def recognize(self, image_path, languages, *, page_number=1):
            text = f"CHAPTER {page_number}"
            return PageResult(
                page_number=page_number, width=200, height=300,
                blocks=[TextBlock(
                    text=text, bbox=(0.1, 0.08, 0.9, 0.14),
                    block_type="text", confidence=98.0, source=TextSource.TESSERACT,
                    line_number=1, paragraph_number=1,
                )],
                source=TextSource.TESSERACT, quality=QualityStatus.HIGH,
                metrics=PageMetrics(mean_ocr_confidence=98.0),
            )

    source = make_scanned_pdf(tmp_path / "chapters.pdf", page_count=2)
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="eng")
    engine = ChapterOcrEngine()
    processor = PdfConversionJobProcessor(engine=engine)
    worker = ConversionWorker(store, processor, worker_id="test-worker")

    # Step 1: Run analysis
    worker.run_once()
    assert store.get_job(job.id).status is JobStatus.READY_FOR_OCR

    # Step 2: Run OCR through to extracting
    worker.run_once()
    assert store.get_job(job.id).status is JobStatus.EXTRACTING

    workspace = store.workspace(job.id)
    p1_structure = workspace.load_page_structure(1)
    p2_structure = workspace.load_page_structure(2)
    assert p1_structure is not None and len(p1_structure) == 1
    assert p2_structure is not None and len(p2_structure) == 1
    assert p1_structure[0]["text"] == "CHAPTER 1"
    assert p2_structure[0]["text"] == "CHAPTER 2"

    # Simulate interruption: global structure analysis exists, but page 2 structure was interrupted
    workspace.page_structure_path(2).unlink()
    assert workspace.load_page_structure(2) is None
    assert workspace.load_structure_analysis() is not None

    # Reset status to READY_FOR_OCR to simulate interrupted job recovery
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, worker_id = NULL WHERE id = ?",
            (JobStatus.READY_FOR_OCR.value, job.id),
        )

    # Worker resumes the job
    worker.run_once()
    assert store.get_job(job.id).status is JobStatus.EXTRACTING

    # Verify page 2 structure was deterministically restored, not left as []
    restored_p2_structure = workspace.load_page_structure(2)
    assert restored_p2_structure is not None
    assert len(restored_p2_structure) == 1
    assert restored_p2_structure[0]["text"] == "CHAPTER 2"
    assert restored_p2_structure[0]["chapter_start"] is True

    # Verify build_document_model produces the exact expected structure
    from conversion.document import build_document_model
    cleaned_pages = {
        1: workspace.load_page_cleaned_paragraphs(1),
        2: workspace.load_page_cleaned_paragraphs(2),
    }
    structure_analysis = workspace.load_structure_analysis()
    page_annotations = {
        1: workspace.load_page_structure(1),
        2: workspace.load_page_structure(2),
    }
    doc = build_document_model(
        cleaned_pages, structure_analysis, page_annotations,
        total_pages=2, title="Recovered Book", language="en"
    )
    assert len(doc.chapters) == 2
    assert doc.chapters[0].title == "CHAPTER 1"
    assert doc.chapters[1].title == "CHAPTER 2"

