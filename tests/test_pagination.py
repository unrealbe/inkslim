from copy import deepcopy

import pytest

from conversion.pagination import detect_repeated_marginalia


def paragraph(text, bbox):
    return {"text": text, "bbox": list(bbox), "paragraph_index": 1}


def test_removes_repeated_header_and_footer_but_preserves_body():
    pages = {
        page: [
            paragraph("My Book — Author", (0.2, 0.03, 0.8, 0.06)),
            paragraph(f"Body content page {page}", (0.1, 0.3, 0.9, 0.6)),
            paragraph("Publisher imprint", (0.2, 0.94, 0.8, 0.97)),
        ]
        for page in range(1, 6)
    }
    analysis, cleaned = detect_repeated_marginalia(pages, total_pages=5)

    assert analysis["removed_paragraphs"] == 10
    assert len(analysis["items"]) == 2
    assert all(len(cleaned[page]) == 1 for page in pages)
    assert cleaned[3][0]["text"] == "Body content page 3"
    assert len(pages[3]) == 3


def test_detects_variable_page_numbers_at_stable_footer_position():
    pages = {
        page: [
            paragraph(str(page), (0.48, 0.95, 0.52, 0.98)),
            paragraph("Actual content", (0.1, 0.25, 0.9, 0.7)),
        ]
        for page in range(1, 7)
    }
    analysis, cleaned = detect_repeated_marginalia(pages, total_pages=6)

    assert analysis["items"][0]["kind"] == "page_number"
    assert analysis["removed_paragraphs"] == 6
    assert all([p["text"] for p in cleaned[page]] == ["Actual content"] for page in pages)


def test_does_not_remove_infrequent_margin_text_or_numeric_body_content():
    pages = {
        1: [paragraph("Chapter 1", (0.2, 0.03, 0.8, 0.06)), paragraph("2026", (0.1, 0.4, 0.2, 0.45))],
        2: [paragraph("Chapter 2", (0.2, 0.03, 0.8, 0.06)), paragraph("2027", (0.1, 0.4, 0.2, 0.45))],
        3: [paragraph("Chapter 3", (0.2, 0.03, 0.8, 0.06)), paragraph("2028", (0.1, 0.4, 0.2, 0.45))],
    }
    analysis, cleaned = detect_repeated_marginalia(pages, total_pages=3)

    assert analysis["removed_paragraphs"] == 0
    assert cleaned == pages


def test_requires_three_distinct_pages_even_for_short_documents():
    pages = {
        1: [paragraph("Running title", (0.2, 0.03, 0.8, 0.06))],
        2: [paragraph("Running title", (0.2, 0.03, 0.8, 0.06))],
    }
    analysis, cleaned = detect_repeated_marginalia(pages, total_pages=2)

    assert analysis["items"] == []
    assert cleaned == pages


def test_repeated_text_inside_body_is_not_removed():
    pages = {
        page: [paragraph("Confidential", (0.1, 0.3, 0.9, 0.4))]
        for page in range(1, 5)
    }
    analysis, cleaned = detect_repeated_marginalia(pages, total_pages=4)

    assert analysis["removed_paragraphs"] == 0
    assert all(len(cleaned[page]) == 1 for page in pages)


def test_invalid_page_count_is_rejected():
    with pytest.raises(ValueError, match="total_pages"):
        detect_repeated_marginalia({}, total_pages=0)
