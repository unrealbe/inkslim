import json

from conversion.lines import ReconstructedLine
from conversion.models import PageResult, QualityStatus, TextBlock, TextSource
from conversion.paragraphs import ReconstructedParagraph
from conversion.pipeline_debug import debug_page_requested, dump_page_pipeline


def test_debug_page_requested_is_opt_in_and_matches_one_page(monkeypatch):
    monkeypatch.delenv("DEBUG_CONVERSION_PAGE", raising=False)
    assert not debug_page_requested(5)

    monkeypatch.setenv("DEBUG_CONVERSION_PAGE", "5")
    assert debug_page_requested(5)
    assert not debug_page_requested(4)

    monkeypatch.setenv("DEBUG_CONVERSION_PAGE", "all")
    assert not debug_page_requested(5)


def test_dump_page_pipeline_writes_ordered_blocks_lines_and_paragraphs(tmp_path):
    result = PageResult(
        page_number=5,
        width=600,
        height=800,
        source=TextSource.NATIVE,
        quality=QualityStatus.HIGH,
        blocks=[
            TextBlock(
                text="There is a scent, that",
                bbox=(0.1, 0.2, 0.5, 0.22),
                block_type="text",
                confidence=None,
                source=TextSource.NATIVE,
                line_number=1,
                paragraph_number=1,
            ),
            TextBlock(
                text="’s all.",
                bbox=(0.5, 0.2, 0.6, 0.22),
                block_type="text",
                confidence=None,
                source=TextSource.NATIVE,
                line_number=2,
                paragraph_number=1,
            ),
        ],
    )
    lines = [
        ReconstructedLine(
            line_index=1,
            text="There is a scent, that’s all.",
            bbox=(0.1, 0.2, 0.6, 0.22),
            confidence=None,
            source="native",
            source_line_number=None,
            source_paragraph_number=1,
        )
    ]
    paragraphs = [
        ReconstructedParagraph(
            paragraph_index=1,
            text="There is a scent, that’s all.",
            bbox=(0.1, 0.2, 0.6, 0.22),
            confidence=None,
            source="native",
            line_indices=(1,),
        )
    ]
    output = tmp_path / "000005-debug.json"

    dump_page_pipeline(result, lines, paragraphs, output)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["page"] == 5
    assert [item["index"] for item in payload["page_result"]["blocks"]] == [0, 1]
    assert [item["text"] for item in payload["page_result"]["blocks"]] == [
        "There is a scent, that",
        "’s all.",
    ]
    assert payload["reconstructed_lines"][0]["text"] == "There is a scent, that’s all."
    assert payload["paragraphs"][0]["text"] == "There is a scent, that’s all."
