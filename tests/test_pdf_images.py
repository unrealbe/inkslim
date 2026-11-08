from __future__ import annotations

from pathlib import Path

import pytest
import pypdfium2 as pdfium
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from conversion.pdf.images import PageImage, PageImages, extract_page_images
from conversion.storage import JobWorkspace


def _make_pdf_with_images(tmp_path: Path) -> Path:
    png_path, jpeg_path = tmp_path / "illustration.png", tmp_path / "photo.jpg"
    Image.new("RGB", (320, 240), "red").save(png_path)
    Image.new("RGB", (640, 480), "blue").save(jpeg_path, quality=90)
    pdf_path = tmp_path / "images.pdf"
    page = canvas.Canvas(str(pdf_path), pagesize=letter)
    page.drawImage(str(png_path), 72, 300, width=240, height=180)
    page.drawImage(str(jpeg_path), 350, 500, width=180, height=135)
    page.showPage()
    page.save()
    return pdf_path


def test_page_image_checkpoint_round_trip_and_safe_path():
    image = PageImage(2, 1, (0.1, 0.2, 0.8, 0.9), 320, 240,
                      "image/png", "images/000002-0001.png", 0.49)
    checkpoint = PageImages(2, (image,), ("IMAGE_NOTE",))
    assert PageImages.from_dict(checkpoint.to_dict()) == checkpoint
    with pytest.raises(ValueError, match="safe relative"):
        PageImage(1, 1, (0, 0, 1, 1), 10, 10, "image/png",
                  "images/../outside.png", 1)


def test_extract_page_images_preserves_png_jpeg_and_normalized_position(tmp_path):
    pdf_path = _make_pdf_with_images(tmp_path)
    workspace = JobWorkspace(tmp_path / "workspace")
    workspace.create()
    with pdfium.PdfDocument(str(pdf_path)) as document:
        page = document[0]
        try:
            result = extract_page_images(page, 1, workspace)
        finally:
            page.close()
    assert len(result.images) == 2
    assert result.detected_count == 2
    assert {image.media_type for image in result.images} == {"image/png", "image/jpeg"}
    assert all(0 < image.coverage < 1 for image in result.images)
    assert all((workspace.root / image.source_path).is_file() for image in result.images)
    assert result.images[0].bbox[0] == pytest.approx(72 / 612, abs=0.02)
    assert result.images[0].bbox[1] == pytest.approx((792 - 480) / 792, abs=0.02)
