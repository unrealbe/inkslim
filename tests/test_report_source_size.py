"""The report must state the size of the source PDF, not of an extracted image."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from conversion.models import PageMetrics, PageResult, QualityStatus, TextBlock, TextSource
from conversion.pdf.ocr_processor import PdfConversionJobProcessor
from conversion.storage import JobStore
from conversion.worker import ConversionWorker


class StubEngine:
    def recognize(self, image_path, languages, *, page_number=1):
        return PageResult(
            page_number=page_number,
            width=612,
            height=792,
            blocks=[
                TextBlock(
                    text="A line of recognised text on a scanned page.",
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


def make_scans(path: Path, pages: int) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    for number in range(pages):
        # Different sizes per page, so the last image is unlike the whole PDF.
        image = Image.new("L", (600 + number * 40, 800), 255)
        ImageDraw.Draw(image).text((50, 50 + number), f"scanned page {number}", fill=0)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        buffer.seek(0)
        pdf.drawImage(ImageReader(buffer), 0, 0, width=612, height=792)
        pdf.showPage()
    pdf.save()
    return path


def test_report_source_pdf_bytes_is_the_size_of_the_pdf_not_of_the_last_image(tmp_path):
    source = make_scans(tmp_path / "scans.pdf", 3)
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="eng")
    worker = ConversionWorker(
        store, PdfConversionJobProcessor(engine=StubEngine()), worker_id="source-size"
    )
    while worker.run_once():
        pass

    finished = store.get_job(job.id)
    report = json.loads(Path(finished.report_path).read_text(encoding="utf-8"))
    assert report["images"]["embedded_in_epub"] >= 1  # the loop over images really ran
    assert report["output"]["source_pdf_bytes"] == source.stat().st_size
    assert report["output"]["epub_bytes"] == Path(finished.output_path).stat().st_size
