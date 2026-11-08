from __future__ import annotations

from pathlib import Path
from textwrap import wrap

from reportlab.pdfgen import canvas

from conversion.models import PageMetrics, PageResult, QualityStatus, TextBlock, TextSource
from conversion.pdf.models import PageDecision
from conversion.pdf.ocr_processor import PdfConversionJobProcessor
from conversion.storage import JobStore
from conversion.worker import ConversionWorker


NATIVE_TEXT = (
    "This is a sufficiently long native PDF paragraph used to validate the "
    "quality of extracted text against an independent OCR result. It contains "
    "several words and punctuation so that token comparison is meaningful."
)


class StubOcrEngine:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[int] = []

    def recognize(self, image_path, languages, *, page_number=1):
        assert Path(image_path).is_file()
        self.calls.append(page_number)
        return PageResult(
            page_number=page_number,
            width=612,
            height=792,
            blocks=[
                TextBlock(
                    text=self.text,
                    bbox=(0.1, 0.1, 0.9, 0.2),
                    block_type="text",
                    confidence=96.0,
                    source=TextSource.TESSERACT,
                    line_number=1,
                    paragraph_number=1,
                )
            ],
            source=TextSource.TESSERACT,
            quality=QualityStatus.HIGH,
            metrics=PageMetrics(mean_ocr_confidence=96.0),
        )


def make_native_pdf(path: Path, text: str = NATIVE_TEXT) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica", 12)
    for line_number, line in enumerate(wrap(text, width=75)):
        pdf.drawString(60, 740 - line_number * 18, line)
    pdf.showPage()
    pdf.save()
    return path


def run_analysis(tmp_path: Path, engine: StubOcrEngine):
    source = make_native_pdf(tmp_path / "native.pdf")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="eng")
    worker = ConversionWorker(
        store,
        PdfConversionJobProcessor(engine=engine, native_text_sample_interval=10),
        worker_id="native-validation",
    )
    assert worker.run_once()
    return store, job, worker


def test_sampled_native_text_is_accepted_when_ocr_agrees(tmp_path):
    engine = StubOcrEngine(NATIVE_TEXT)
    store, job, _ = run_analysis(tmp_path, engine)

    analysis = store.workspace(job.id).load_page_analysis(1)
    checkpoint = store.workspace(job.id).load_page_result(1)

    assert engine.calls == [1]
    assert analysis.decision is PageDecision.USE_NATIVE
    assert any(item.startswith("NATIVE_OCR_SIMILARITY:") for item in analysis.warnings)
    assert checkpoint is not None
    assert checkpoint.source is TextSource.NATIVE
    assert checkpoint.blocks
    assert all(block.source is TextSource.NATIVE for block in checkpoint.blocks)
    assert store.workspace(job.id).load_page_lines(1)


def test_native_text_is_routed_to_ocr_when_sample_disagrees(tmp_path):
    engine = StubOcrEngine("This OCR output is unrelated and does not match the PDF page.")
    store, job, worker = run_analysis(tmp_path, engine)

    analysis = store.workspace(job.id).load_page_analysis(1)
    assert analysis.decision is PageDecision.OCR_REQUIRED
    assert "NATIVE_OCR_MISMATCH" in analysis.warnings
    assert store.workspace(job.id).load_page_result(1).source is TextSource.TESSERACT

    # The regular OCR phase must reuse the validation result instead of running OCR twice.
    assert worker.run_once()
    assert engine.calls == [1]



def test_native_text_result_contains_normalized_geometry_and_line_checkpoints(tmp_path):
    text = (
        "The first line contains native PDF text with selectable characters. "
        "The second line continues the same paragraph with more readable words."
    )
    source = make_native_pdf(tmp_path / "native-lines.pdf", text)
    store = JobStore(tmp_path / "native-lines.sqlite3", tmp_path / "native-lines-jobs")
    job = store.create_job(source, source.name, language="eng")
    worker = ConversionWorker(
        store,
        PdfConversionJobProcessor(native_text_sample_interval=0),
        worker_id="native-lines",
    )

    assert worker.run_once()

    workspace = store.workspace(job.id)
    result = workspace.load_page_result(1)
    assert result is not None
    assert result.source is TextSource.NATIVE
    assert result.quality is QualityStatus.HIGH
    assert "first line" in " ".join(block.text for block in result.blocks).lower()
    assert "second line" in " ".join(block.text for block in result.blocks).lower()
    assert len(result.blocks) >= 2
    assert all(block.confidence is None for block in result.blocks)
    assert all(
        0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1
        for block in result.blocks
        for x0, y0, x1, y1 in [block.bbox]
    )
    lines = workspace.load_page_lines(1)
    assert lines is not None
    assert len(lines) == len(result.blocks)
