import io
import sys
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


@pytest.fixture()
def client():
    app.AUTH_USER = app.AUTH_PASS = ""
    return app.app.test_client()


def make_test_pdf(path):
    image = Image.effect_noise((2400, 2400), 80).convert("RGB")
    image_buffer = io.BytesIO()
    image.save(image_buffer, format="JPEG", quality=96)
    image_buffer.seek(0)
    image.close()

    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setTitle("Inkslim PDF test")
    pdf.drawString(72, 740, "This text should remain searchable in Optimize PDF mode.")
    pdf.drawImage(ImageReader(image_buffer), 40, 100, width=530, height=530)
    pdf.showPage()
    pdf.drawString(72, 740, "Second page")
    pdf.showPage()
    pdf.save()
    return path.read_bytes()


def post_pdf(client, content, filename="sample.pdf", **form):
    data = {"file": (io.BytesIO(content), filename), **form}
    return client.post("/shrink", data=data, content_type="multipart/form-data")


def test_pdf_optimize_keeps_selectable_text(client, tmp_path):
    original = make_test_pdf(tmp_path / "source.pdf")
    response = post_pdf(
        client,
        original,
        "Sample Document.pdf",
        pdf_mode="optimize",
        pdf_dpi="72",
        quality="60",
        min_kb="1",
        gray="1",
    )
    assert response.status_code == 200, response.get_json(silent=True)
    assert response.mimetype == "application/pdf"
    assert "Sample Document-small.pdf" in response.headers["Content-Disposition"]

    output_path = tmp_path / "optimized.pdf"
    output_path.write_bytes(response.data)
    pdf = pdfium.PdfDocument(str(output_path))
    try:
        assert len(pdf) == 2
        page = pdf[0]
        text_page = page.get_textpage()
        try:
            assert "searchable" in text_page.get_text_range()
        finally:
            text_page.close()
            page.close()
    finally:
        pdf.close()


def test_pdf_ereader_mode_rasterizes_pages(client, tmp_path):
    original = make_test_pdf(tmp_path / "source.pdf")
    response = post_pdf(
        client,
        original,
        "Sample Document.pdf",
        pdf_mode="ereader",
        width="768",
        height="1024",
        quality="70",
        gray="1",
    )
    assert response.status_code == 200, response.get_json(silent=True)

    output_path = tmp_path / "ereader.pdf"
    output_path.write_bytes(response.data)
    with pikepdf.open(output_path) as pdf:
        assert len(pdf.pages) == 2
        for page in pdf.pages:
            text = page.Resources.get("/XObject", {})
            assert any(
                obj.get("/Subtype") == pikepdf.Name("/Image")
                for obj in text.values()
            )

    rendered = pdfium.PdfDocument(str(output_path))
    try:
        page = rendered[0]
        text_page = page.get_textpage()
        try:
            assert text_page.get_text_range().strip() == ""
        finally:
            text_page.close()
            page.close()
    finally:
        rendered.close()


def test_rejects_invalid_pdf(client):
    response = post_pdf(client, b"not a PDF", "invalid.pdf")
    assert response.status_code == 422
    assert "valid PDF" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Regression tests for behaviour that the first tests did not check:
# images must really shrink, colours must survive, and results must never grow.
# ---------------------------------------------------------------------------

import numpy as np  # noqa: E402  (installed with pytest extras, see requirements-dev.txt)


def noisy_image(size, mode="RGB"):
    base = Image.effect_noise(size, 70).convert("RGB")
    return base if mode == "RGB" else base.convert(mode)


def jpeg_bytes(image, quality):
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def pdf_with_image(path, image_data, pages=2, pagesize=(595, 842)):
    pdf = canvas.Canvas(str(path), pagesize=pagesize)
    for _ in range(pages):
        pdf.drawImage(ImageReader(io.BytesIO(image_data)), 0, 0, width=pagesize[0], height=pagesize[1])
        pdf.showPage()
    pdf.save()
    return path.read_bytes()


def first_image(pdf_bytes):
    """Return (width, height, filter) of the first image. Read while the PDF is still open."""
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == pikepdf.Name("/Image"):
                return int(obj.Width), int(obj.Height), str(obj.get("/Filter"))
    raise AssertionError("no image found in PDF")


def render_patch(pdf_bytes):
    document = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    try:
        bitmap = document[0].render(scale=0.25)
        array = np.asarray(bitmap.to_pil().convert("RGB"), dtype=float)
        bitmap.close()
        return array
    finally:
        document.close()


def test_optimize_really_downsamples_and_shrinks(client, tmp_path):
    original = pdf_with_image(tmp_path / "big.pdf", jpeg_bytes(noisy_image((2480, 3508)), 90))
    response = post_pdf(client, original, "big.pdf", pdf_mode="optimize", pdf_dpi="150", quality="75", min_kb="1")
    assert response.status_code == 200, response.get_json(silent=True)
    assert len(response.data) < len(original) * 0.5
    width, height, _ = first_image(response.data)
    assert width <= 1240 + 1 and height <= 1754 + 1  # A4 at 150 dpi


def test_optimize_recompresses_high_quality_images_without_resizing(client, tmp_path):
    # Already at 150 dpi, but saved at JPEG quality 97: must still shrink.
    original = pdf_with_image(tmp_path / "hq.pdf", jpeg_bytes(noisy_image((1240, 1754)), 97))
    response = post_pdf(client, original, "hq.pdf", pdf_mode="optimize", pdf_dpi="150", quality="75", min_kb="1")
    assert response.status_code == 200, response.get_json(silent=True)
    assert len(response.data) < len(original) * 0.8


def test_optimize_cmyk_colours_are_preserved(client, tmp_path):
    # Regression: re-encoding CMYK JPEGs without the right /Decode array made them near-black.
    image = Image.new("RGB", (1500, 2100), (250, 250, 250))
    for index, colour in enumerate([(220, 30, 30), (30, 170, 50), (40, 60, 210), (240, 210, 30)]):
        block = Image.new("RGB", (300, 800), colour)
        image.paste(block, (60 + index * 340, 300))
    image = Image.blend(image, noisy_image(image.size), 0.05)
    original = pdf_with_image(tmp_path / "cmyk.pdf", jpeg_bytes(image.convert("CMYK"), 92), pages=1)

    response = post_pdf(client, original, "cmyk.pdf", pdf_mode="optimize", pdf_dpi="100", quality="75", min_kb="1")
    assert response.status_code == 200, response.get_json(silent=True)

    before, after = render_patch(original), render_patch(response.data)
    assert float(np.abs(before - after).mean()) < 25  # near-black output would give about 190


def test_ereader_mode_that_would_grow_the_file_is_rejected(client, tmp_path):
    pdf = canvas.Canvas(str(tmp_path / "text.pdf"), pagesize=(595, 842))
    for page in range(10):
        for line in range(50):
            pdf.drawString(50, 800 - line * 14, f"Page {page} line {line}: plain text that compresses very well.")
        pdf.showPage()
    pdf.save()
    original = (tmp_path / "text.pdf").read_bytes()

    response = post_pdf(client, original, "text.pdf", pdf_mode="ereader", quality="75", gray="1")
    assert response.status_code == 422
    assert "Optimize PDF" in response.get_json()["error"]


def test_ereader_images_are_not_ascii85_encoded(client, tmp_path):
    original = pdf_with_image(tmp_path / "scan.pdf", jpeg_bytes(noisy_image((2480, 3508)), 90))
    response = post_pdf(client, original, "scan.pdf", pdf_mode="ereader", quality="70", gray="1")
    assert response.status_code == 200, response.get_json(silent=True)
    _, _, image_filter = first_image(response.data)
    assert "/DCTDecode" in image_filter  # sanity check: the filter was really read
    assert "/ASCII85Decode" not in image_filter
