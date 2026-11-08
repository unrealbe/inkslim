from conversion.lines import ReconstructedLine
from conversion.paragraphs import reconstruct_paragraphs


def line(index, text, bbox, paragraph=None, confidence=90, source="tesseract"):
    return ReconstructedLine(
        line_index=index,
        text=text,
        bbox=bbox,
        confidence=confidence,
        source=source,
        source_line_number=index,
        source_paragraph_number=paragraph,
    )


def test_reconstruct_paragraphs_joins_continuation_lines_without_dehyphenating():
    paragraphs = reconstruct_paragraphs([
        line(1, "Een eerste para-", (0.1, 0.1, 0.8, 0.13)),
        line(2, "graaf loopt verder.", (0.1, 0.145, 0.75, 0.175)),
        line(3, "Een tweede alinea.", (0.1, 0.24, 0.7, 0.27)),
    ])

    assert [paragraph.text for paragraph in paragraphs] == [
        "Een eerste para- graaf loopt verder.",
        "Een tweede alinea.",
    ]
    assert [paragraph.line_indices for paragraph in paragraphs] == [(1, 2), (3,)]
    assert paragraphs[0].bbox == (0.1, 0.1, 0.8, 0.175)


def test_reconstruct_paragraphs_uses_source_paragraph_metadata():
    paragraphs = reconstruct_paragraphs([
        line(1, "Eerste regel.", (0.1, 0.1, 0.8, 0.13), paragraph=1),
        line(2, "Tweede alinea.", (0.1, 0.145, 0.8, 0.175), paragraph=2),
    ])

    assert [paragraph.text for paragraph in paragraphs] == [
        "Eerste regel.", "Tweede alinea."
    ]


def test_reconstruct_paragraphs_splits_on_first_line_indent():
    paragraphs = reconstruct_paragraphs([
        line(1, "Vorige alinea.", (0.1, 0.1, 0.8, 0.13)),
        line(2, "Nieuwe alinea.", (0.16, 0.145, 0.8, 0.175)),
    ])

    assert len(paragraphs) == 2


def test_reconstruct_paragraphs_aggregates_confidence_and_mixed_sources():
    paragraphs = reconstruct_paragraphs([
        line(1, "Native regel", (0.1, 0.1, 0.8, 0.13), confidence=None, source="native"),
        line(2, "OCR vervolg", (0.1, 0.145, 0.8, 0.175), confidence=80, source="tesseract"),
    ])

    assert len(paragraphs) == 1
    assert paragraphs[0].source == "mixed"
    assert paragraphs[0].confidence == 80
    assert paragraphs[0].to_dict()["line_indices"] == [1, 2]


def test_reconstruct_paragraphs_handles_empty_input():
    assert reconstruct_paragraphs([]) == []
