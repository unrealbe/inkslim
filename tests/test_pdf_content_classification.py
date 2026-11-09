from __future__ import annotations

from conversion.pdf.content_classification import classify_page_content
from conversion.pdf.images import PageImage, PageImages
from conversion.pdf.models import PageAnalysis, PageDecision


def _analysis(page=1, words=0, native=False, decision=PageDecision.OCR_REQUIRED):
    return PageAnalysis(
        page_number=page, width=612, height=792, character_count=words * 5,
        word_count=words, image_count=0, has_native_text=native,
        native_text_quality=0.9 if native else None, decision=decision,
    )


def _images(*coverages):
    return PageImages(
        page_number=1,
        images=tuple(
            PageImage(1, index, (0.05, 0.05, 0.95, 0.95), 100, 100,
                      "image/png", f"images/000001-{index:04d}.png", coverage)
            for index, coverage in enumerate(coverages, start=1)
        ),
        detected_count=len(coverages),
    )


def test_first_page_dominant_artwork_is_only_a_cover_candidate():
    result = classify_page_content(_analysis(), _images(0.8))
    assert result["type"] == "cover_candidate"
    assert result["confidence"] < 0.9
    assert result["signals"]["technical_ocr_decision"] == "ocr_required"


def test_ocr_text_page_is_scanned_text_not_graphic():
    result = classify_page_content(
        _analysis(page=4), _images(0.9),
        recognized_text=" ".join(["Readable"] * 45),
        text_source="tesseract",
    )
    assert result["type"] == "scanned_text"


def test_text_and_illustration_is_mixed():
    result = classify_page_content(
        _analysis(page=8, words=120, native=True, decision=PageDecision.USE_NATIVE),
        _images(0.25),
        recognized_text=" ".join(["Chapter"] * 120),
        text_source="native",
    )
    assert result["type"] == "mixed"


def test_promotional_language_and_images_is_advert_candidate():
    result = classify_page_content(
        _analysis(page=30, words=35, native=True, decision=PageDecision.USE_NATIVE),
        _images(0.2, 0.1),
        recognized_text="Newsletter subscribe visit our website for more information",
        text_source="native",
    )
    assert result["type"] == "advert_candidate"


def test_incomplete_image_detection_caps_confidence():
    incomplete = PageImages(3, (), ("IMAGE_OBJECT_SCAN_FAILED",), None)
    result = classify_page_content(_analysis(page=3), incomplete)
    assert result["confidence"] <= 0.45
    assert "Image-object detection is incomplete" in result["reasons"]


def test_empty_page_classification():
    result = classify_page_content(
        _analysis(page=9, decision=PageDecision.EMPTY), PageImages(9, detected_count=0)
    )
    assert result["type"] == "empty"
