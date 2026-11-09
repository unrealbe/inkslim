"""Conservative, metadata-only semantic classification of PDF pages.

The classifier describes likely page content; it never changes OCR decisions,
text reconstruction, or image inclusion.
"""
from __future__ import annotations

import re
from typing import Any

from .models import PageAnalysis, PageDecision
from .images import PageImages

_ADVERT_TERMS = re.compile(
    r"\\b(?:newsletter|subscribe|subscription|sign up|follow us|"
    r"visit our|www\\.|http[s]?://|buy now|order now|discover more|"
    r"read more|available now|advertisement)\\b",
    re.IGNORECASE,
)
_MIN_BODY_WORDS = 20


def classify_page_content(
    analysis: PageAnalysis,
    images: PageImages | None,
    *,
    recognized_text: str = "",
    text_source: str | None = None,
) -> dict[str, Any]:
    """Return a conservative page-type estimate with confidence and evidence."""
    words = len(re.findall(r"[^\\W_]+", recognized_text, re.UNICODE))
    if words == 0:
        words = analysis.word_count
    has_text = words > 0 or analysis.has_native_text
    image_count = len(images.images) if images is not None else None
    coverage = min(1.0, sum(item.coverage for item in images.images)) if images else 0.0
    max_coverage = max((item.coverage for item in images.images), default=0.0) if images else 0.0
    reasons: list[str] = []
    page_type = "unknown"
    confidence = 0.35

    if not has_text and images is not None and images.detected_count == 0:
        page_type, confidence = "empty", 0.95
        reasons.append("No text or embedded images were detected")
    elif (
        analysis.page_number == 1
        and max_coverage >= 0.45
        and words < _MIN_BODY_WORDS
    ):
        page_type, confidence = "cover_candidate", 0.78
        reasons.append("First page contains a dominant image and little text")
    elif (
        _ADVERT_TERMS.search(recognized_text or "")
        and (image_count or 0) >= 1
    ):
        page_type, confidence = "advert_candidate", 0.68
        reasons.append("Promotional or navigation language appears alongside images")
    elif text_source == "tesseract" and words >= _MIN_BODY_WORDS:
        page_type, confidence = "scanned_text", 0.88
        reasons.append("Substantial text was recovered by OCR")
        if image_count:
            reasons.append("The page also contains embedded image objects")
    elif has_text and image_count and words >= 3:
        page_type, confidence = "mixed", 0.82
        reasons.append("Substantial text and embedded images coexist")
    elif has_text and not image_count:
        page_type, confidence = "text", 0.92
        reasons.append("Text is present and no embedded images were detected")
    elif image_count and max_coverage >= 0.20 and words < _MIN_BODY_WORDS:
        page_type, confidence = "illustration_candidate", 0.72
        reasons.append("A sizeable image dominates a page with little text")
    elif has_text:
        page_type, confidence = "text", 0.65
        reasons.append("Text is present, but evidence is insufficient for a more specific type")
    elif image_count:
        page_type, confidence = "illustration_candidate", 0.55
        reasons.append("Images are present but their semantic role is uncertain")
    else:
        reasons.append("Available page signals are incomplete or ambiguous")

    if images is None or images.detected_count is None:
        confidence = min(confidence, 0.45)
        reasons.append("Image-object detection is incomplete")
    elif images.detected_count != len(images.images):
        confidence = min(confidence, 0.60)
        reasons.append("Some detected image objects were not extracted")

    return {
        "type": page_type,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "signals": {
            "text_word_count": words,
            "text_source": text_source or (
                "native" if analysis.has_native_text else None
            ),
            "detected_image_count": image_count,
            "extracted_image_count": len(images.images) if images else None,
            "image_coverage": round(coverage, 4),
            "largest_image_coverage": round(max_coverage, 4),
            "technical_ocr_decision": analysis.decision.value,
        },
    }
