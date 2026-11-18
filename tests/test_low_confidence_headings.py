"""Fragments of artwork read by OCR must not become chapters or navigation entries."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from conversion.models import PageMetrics, PageResult, QualityStatus, TextBlock, TextSource
from conversion.pdf.ocr_processor import PdfConversionJobProcessor
from conversion.storage import JobStore
from conversion.worker import ConversionWorker

GARBAGE = "4 x Pa > a xoeI zm"
PROSE = "The harbour lay quiet under a grey sky while the fishing boats waited for the tide."


class ArtworkEngine:
    """Reads a stylised advertisement: one confident sentence and one noisy numbered fragment."""

    def __init__(self, noisy_confidence: float):
        self.noisy_confidence = noisy_confidence

    def recognize(self, image_path, languages, *, page_number=1):
        def block(text, bbox, confidence, line):
            return TextBlock(
                text=text, bbox=bbox, block_type="text", confidence=confidence,
                source=TextSource.TESSERACT, line_number=line, paragraph_number=line,
            )

        return PageResult(
            page_number=page_number, width=612, height=792,
            blocks=[
                block(GARBAGE, (0.1, 0.10, 0.5, 0.13), self.noisy_confidence, 1),
                block(PROSE, (0.1, 0.40, 0.9, 0.44), 95.0, 2),
            ],
            source=TextSource.TESSERACT, quality=QualityStatus.HIGH,
            metrics=PageMetrics(mean_ocr_confidence=80.0),
        )


def make_scan(path: Path) -> Path:
    image = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(image).text((50, 50), "advert", fill=0)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.drawImage(ImageReader(buffer), 0, 0, width=612, height=792)
    pdf.showPage()
    pdf.save()
    return path


def convert(tmp_path: Path, noisy_confidence: float):
    source = make_scan(tmp_path / "advert.pdf")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="eng")
    worker = ConversionWorker(
        store, PdfConversionJobProcessor(engine=ArtworkEngine(noisy_confidence)), worker_id="h"
    )
    while worker.run_once():
        pass
    finished = store.get_job(job.id)
    with zipfile.ZipFile(finished.output_path) as epub:
        nav = epub.read("OEBPS/nav.xhtml").decode("utf-8")
        chapters = "".join(
            epub.read(name).decode("utf-8")
            for name in sorted(epub.namelist())
            if re.search(r"chapter_\d+\.xhtml$", name)
        )
    return nav, chapters


def test_noisy_numbered_fragment_is_neither_a_chapter_nor_a_navigation_entry(tmp_path):
    nav, chapters = convert(tmp_path, noisy_confidence=60.0)

    assert "xoeI" not in nav
    # The text itself is kept as an ordinary paragraph; only its heading status is withheld.
    assert "xoeI" in chapters
    assert not re.search(r"<h[1-6][^>]*>[^<]*xoeI", chapters)
    assert PROSE in chapters


def test_the_same_fragment_read_confidently_still_becomes_a_chapter(tmp_path):
    nav, _ = convert(tmp_path, noisy_confidence=92.0)

    assert "xoeI" in nav
