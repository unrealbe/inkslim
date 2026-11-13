from copy import deepcopy

import pytest

from conversion.structure import detect_document_structure


def paragraph(index, text, bbox=(0.1, 0.08, 0.9, 0.14)):
    return {
        "paragraph_index": index, "text": text, "bbox": list(bbox),
        "confidence": 95.0, "source": "native", "line_indices": [index],
    }


def test_detects_explicit_chapter_labels_and_indexes_them():
    pages = {
        1: [paragraph(1, "CHAPTER 1"), paragraph(2, "Ordinary prose continues here.", (0.1, 0.2, 0.9, 0.4))],
        4: [paragraph(1, "Hoofdstuk 2: De terugkeer")],
    }
    analysis, annotations = detect_document_structure(pages, total_pages=4)
    assert analysis["chapter_count"] == 2
    assert [item["title"] for item in analysis["chapters"]] == ["CHAPTER 1", "Hoofdstuk 2: De terugkeer"]
    assert annotations[1][0]["chapter_index"] == 1
    assert annotations[4][0]["chapter_index"] == 2


def test_detects_numbered_and_all_caps_headings_but_not_body_sentences():
    pages = {1: [
        paragraph(1, "1 Introduction"),
        paragraph(2, "BACKGROUND AND SCOPE", (0.1, 0.3, 0.9, 0.36)),
        paragraph(3, "This is a normal sentence.", (0.1, 0.45, 0.9, 0.55)),
    ]}
    analysis, annotations = detect_document_structure(pages, total_pages=1)
    assert analysis["heading_count"] == 2
    assert [item["kind"] for item in annotations[1]] == ["chapter_heading", "display_heading"]
    assert annotations[1][0]["chapter_start"] is True
    assert annotations[1][1]["confidence"] < annotations[1][0]["confidence"]


def test_recognizes_standalone_section_labels_without_starting_chapter():
    analysis, annotations = detect_document_structure({1: [paragraph(1, "Prologue")]}, total_pages=1)
    assert analysis["chapter_count"] == 0
    assert annotations[1][0]["kind"] == "section_heading"
    assert annotations[1][0]["chapter_start"] is False


def test_does_not_classify_long_or_lower_page_body_text():
    pages = {1: [
        paragraph(1, "THIS IS A VERY LONG BLOCK OF ALL CAPS BODY TEXT THAT SHOULD NOT BE A HEADING"),
        paragraph(2, "IMPORTANT", (0.1, 0.9, 0.9, 0.95)),
        paragraph(3, "A short sentence.", (0.1, 0.3, 0.9, 0.4)),
    ]}
    analysis, annotations = detect_document_structure(pages, total_pages=1)
    assert analysis["heading_count"] == 0
    assert annotations[1] == []


def test_preserves_source_paragraphs_and_rejects_invalid_document_size():
    source = [paragraph(1, "CHAPTER 1")]
    before = deepcopy(source)
    detect_document_structure({1: source}, total_pages=1)
    assert source == before
    with pytest.raises(ValueError, match="total_pages"):
        detect_document_structure({}, total_pages=0)


def test_accepts_zero_indexed_paragraph():
    pages = {1: [paragraph(0, "CHAPTER 1")]}
    analysis, annotations = detect_document_structure(pages, total_pages=1)
    assert analysis["chapter_count"] == 1
    assert annotations[1][0]["paragraph_index"] == 0


def test_rejects_top_edge_or_extreme_margin_text():
    pages = {1: [paragraph(1, "CHAPTER 1", bbox=(0.1, 0.0, 0.9, 0.015))]}
    analysis, annotations = detect_document_structure(pages, total_pages=1)
    assert analysis["chapter_count"] == 0
def test_contents_page_is_excluded_and_cannot_create_false_chapters():
    pages = {
        1: [
            paragraph(0, "Contents"),
            paragraph(1, "4 x Pa > a x oe I z m ........ 4"),
            paragraph(2, "CHAPTER 1 The Beginning ........ 5"),
            paragraph(3, "CHAPTER 2 The Journey ........ 12"),
        ],
        5: [
            paragraph(0, "CHAPTER 1"),
            paragraph(1, "The actual story begins here.", (0.1, 0.2, 0.9, 0.4)),
        ],
    }

    analysis, annotations = detect_document_structure(pages, total_pages=35)

    assert analysis["schema_version"] == 2
    assert analysis["page_roles"] == {"1": "contents"}
    assert analysis["excluded_pages"] == [1]
    assert analysis["exclusion_reasons"]["1"] == "contents_heading_and_page_numbered_entries"
    assert annotations[1] == []
    assert analysis["chapter_count"] == 1
    assert analysis["chapters"][0]["page_number"] == 5
    assert analysis["chapters"][0]["title"] == "CHAPTER 1"


def test_repeated_page_numbered_entries_can_identify_contents_without_label():
    pages = {
        2: [
            paragraph(0, "A Beginning ........ 3"),
            paragraph(1, "The Long Road ........ 8"),
            paragraph(2, "The Return ........ 21"),
            paragraph(3, "Afterword ........ 34"),
        ],
        3: [paragraph(0, "Ordinary prose continues here.", (0.1, 0.2, 0.9, 0.4))],
    }

    analysis, _ = detect_document_structure(pages, total_pages=40)

    assert analysis["page_roles"] == {"2": "contents"}
    assert analysis["excluded_pages"] == [2]


def test_copyright_page_is_excluded_only_with_multiple_metadata_signals():
    pages = {
        1: [
            paragraph(0, "Copyright © 2024 Example Publisher"),
            paragraph(1, "All rights reserved. ISBN 978-1-2345-6789-0"),
        ],
        2: [paragraph(0, "A meaningful dedication to my family.")],
    }

    analysis, annotations = detect_document_structure(pages, total_pages=30)

    assert analysis["page_roles"] == {"1": "copyright"}
    assert analysis["excluded_pages"] == [1]
    assert annotations[1] == []
    assert 2 not in analysis["page_roles"]


def test_late_contents_like_page_is_not_excluded():
    pages = {
        12: [
            paragraph(0, "Contents"),
            paragraph(1, "Chapter One ........ 15"),
            paragraph(2, "Chapter Two ........ 25"),
        ],
    }

    analysis, _ = detect_document_structure(pages, total_pages=40)

    assert analysis["excluded_pages"] == []
