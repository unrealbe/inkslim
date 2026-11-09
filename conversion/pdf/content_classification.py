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
    r"\b(?:newsletter|subscribe|subscription|sign up|follow us|"
    r"visit our|www\.|http[s]?://|buy now|order now|"
    r"advertisement)\b",
    re.IGNORECASE,
)
_MIN_BODY_WORDS = 20


def _rectangle_union_coverage(images: PageImages) -> float:
    """Return the union area of normalized image rectangles, without overlap double-counting."""
    rectangles = []
    for item in images.images:
        x0, y0, x1, y1 = item.bbox
        if x1 > x0 and y1 > y0:
            rectangles.append((max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1)))

    if not rectangles:
        return 0.0

    x_edges = sorted({edge for rect in rectangles for edge in (rect[0], rect[2])})
    area = 0.0
    for left, right in zip(x_edges, x_edges[1:]):
        if right <= left:
            continue
        intervals = sorted(
            (top, bottom)
            for x0, top, x1, bottom in rectangles
            if x0 < right and x1 > left
        )
        covered_y = 0.0
        if intervals:
            start, end = intervals[0]
            for next_start, next_end in intervals[1:]:
                if next_start <= end:
                    end = max(end, next_end)
                else:
                    covered_y += max(0.0, end - start)
                    start, end = next_start, next_end
            covered_y += max(0.0, end - start)
        area += (right - left) * covered_y
    return min(1.0, max(0.0, area))


def classify_page_content(
    analysis: PageAnalysis,
    images: PageImages | None,
    *,
    recognized_text: str = "",
    text_source: str | None = None,
) -> dict[str, Any]:
    """Return a conservative page-type estimate with confidence and evidence."""
    words = len(re.findall(r"[^\W_]+", recognized_text, re.UNICODE))
    if words == 0 and text_source != "tesseract":
        words = analysis.word_count
    has_text = words > 0 or (
        analysis.has_native_text and text_source != "tesseract"
    )
    image_count = images.detected_count if images is not None else analysis.image_count
    coverage = _rectangle_union_coverage(images) if images is not None else 0.0
    max_coverage = max((item.coverage for item in images.images), default=0.0) if images else 0.0
    reasons: list[str] = []
    page_type = "unknown"
    confidence = 0.35

    if (
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
        reasons.append(
            "No text or embedded images were detected, but vector drawing content is not inspected"
            if not has_text and image_count == 0
            else "Available page signals are incomplete or ambiguous"
        )

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
            "extracted_image_count": len(images.images) if images is not None else None,
            "image_coverage": round(coverage, 4),
            "largest_image_coverage": round(max_coverage, 4),
            "technical_ocr_decision": analysis.decision.value,
        },
    }
