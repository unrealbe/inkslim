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


def _images(*coverages, page=1):
    return PageImages(
        page_number=page,
        images=tuple(
            PageImage(page, index, (0.0, 0.0, coverage, 1.0), 100, 100,
                      "image/png", f"images/{page:06d}-{index:04d}.png", coverage)
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
        _analysis(page=4), _images(0.9, page=4),
        recognized_text=" ".join(["Readable"] * 45),
        text_source="tesseract",
    )
    assert result["type"] == "scanned_text"


def test_text_and_illustration_is_mixed():
    result = classify_page_content(
        _analysis(page=8, words=120, native=True, decision=PageDecision.USE_NATIVE),
        _images(0.25, page=8),
        recognized_text=" ".join(["Chapter"] * 120),
        text_source="native",
    )
    assert result["type"] == "mixed"


def test_promotional_language_and_images_is_advert_candidate():
    result = classify_page_content(
        _analysis(page=30, words=35, native=True, decision=PageDecision.USE_NATIVE),
        _images(0.2, 0.1, page=30),
        recognized_text="Newsletter subscribe visit our website for more information",
        text_source="native",
    )
    assert result["type"] == "advert_candidate"


def test_incomplete_image_detection_caps_confidence():
    incomplete = PageImages(3, (), ("IMAGE_OBJECT_SCAN_FAILED",), None)
    result = classify_page_content(
        _analysis(page=3, words=120, native=True, decision=PageDecision.USE_NATIVE),
        incomplete,
        recognized_text=" ".join(["Chapter"] * 120),
        text_source="native",
    )
    assert result["type"] == "text"
    assert result["confidence"] <= 0.45
    assert "Image-object detection is incomplete" in result["reasons"]


def test_page_without_text_or_image_objects_is_not_assumed_empty():
    result = classify_page_content(
        _analysis(page=9, decision=PageDecision.EMPTY), PageImages(9, detected_count=0)
    )
    assert result["type"] == "unknown"
    assert result["confidence"] < 0.5
    assert "vector drawing content is not inspected" in result["reasons"][0]


def test_overlapping_image_rectangles_do_not_double_count_coverage():
    result = classify_page_content(
        _analysis(page=10),
        _images(0.5, 0.5, page=10),
    )
    assert result["signals"]["image_coverage"] == 0.5



def test_empty_ocr_result_does_not_reuse_rejected_native_text():
    result = classify_page_content(
        _analysis(page=5, words=35, native=True),
        PageImages(5, detected_count=0),
        recognized_text="",
        text_source="tesseract",
    )
    assert result["type"] == "unknown"
    assert result["signals"]["text_word_count"] == 0
