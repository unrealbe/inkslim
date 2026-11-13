from copy import deepcopy

from conversion.stitching import stitch_page_paragraphs


def paragraph(index, text, bbox):
    return {
        "paragraph_index": index,
        "text": text,
        "bbox": list(bbox),
        "confidence": 95.0,
        "source": "native",
        "line_indices": [index],
    }


def context(*, images=0, decision="use_native"):
    return {"image_count": images, "decision": decision}


def test_stitches_clear_lowercase_continuation_across_page_boundary():
    original = {
        1: [paragraph(4, "the fleet’s instinctive response to these delicate tremors", (0.1, 0.72, 0.9, 0.94))],
        2: [paragraph(1, "is to reach out and pull the ship into alignment.", (0.1, 0.04, 0.9, 0.22)),
            paragraph(2, "A new paragraph starts here.", (0.1, 0.3, 0.9, 0.42))],
    }
    snapshot = deepcopy(original)
    pages, analysis = stitch_page_paragraphs(
        original, {1: context(), 2: context()}, total_pages=2
    )

    assert pages[1][0]["text"] == (
        "the fleet’s instinctive response to these delicate tremors "
        "is to reach out and pull the ship into alignment."
    )
    assert pages[1][0]["paragraph_index"] == 4
    assert pages[1][0]["bbox"] is None
    assert pages[2] == original[2][1:]
    assert analysis["stitched_count"] == 1
    assert original == snapshot


def test_does_not_stitch_when_previous_fragment_ends_a_sentence():
    pages, analysis = stitch_page_paragraphs(
        {
            1: [paragraph(1, "The sentence ends here.", (0.1, 0.72, 0.9, 0.94))],
            2: [paragraph(1, "A new sentence starts here.", (0.1, 0.04, 0.9, 0.22))],
        },
        {1: context(), 2: context()},
        total_pages=2,
    )
    assert len(pages[1]) == len(pages[2]) == 1
    assert analysis["stitched_count"] == 0


def test_does_not_stitch_new_paragraph_or_non_edge_fragments():
    cases = [
        ("unfinished phrase", "A new paragraph", (0.1, 0.04, 0.9, 0.22)),
        ("unfinished phrase", "continuation", (0.1, 0.5, 0.9, 0.7)),
        ("unfinished phrase.", "continuation", (0.1, 0.04, 0.9, 0.22)),
    ]
    for previous, following, next_box in cases:
        pages, analysis = stitch_page_paragraphs(
            {
                1: [paragraph(1, previous, (0.1, 0.72, 0.9, 0.94))],
                2: [paragraph(1, following, next_box)],
            },
            {1: context(), 2: context()},
            total_pages=2,
        )
        assert len(pages[1]) == len(pages[2]) == 1
        assert analysis["stitched_count"] == 0


def test_image_or_empty_page_is_a_hard_boundary():
    for page_context in (
        {1: context(images=1), 2: context()},
        {1: context(), 2: context(decision="empty")},
    ):
        pages, analysis = stitch_page_paragraphs(
            {
                1: [paragraph(1, "unfinished phrase", (0.1, 0.72, 0.9, 0.94))],
                2: [paragraph(1, "continuation", (0.1, 0.04, 0.9, 0.22))],
            },
            page_context,
            total_pages=2,
        )
        assert len(pages[1]) == len(pages[2]) == 1
        assert analysis["stitched_count"] == 0


def test_does_not_stitch_when_either_bbox_is_missing():
    for missing_page in (1, 2):
        first = paragraph(1, "unfinished phrase", (0.1, 0.72, 0.9, 0.94))
        second = paragraph(1, "continuation", (0.1, 0.04, 0.9, 0.22))
        if missing_page == 1:
            first["bbox"] = None
        else:
            second["bbox"] = None
        pages, analysis = stitch_page_paragraphs(
            {1: [first], 2: [second]},
            {1: context(), 2: context()},
            total_pages=2,
        )
        assert analysis["stitched_count"] == 0
        assert pages[1][0]["text"] == "unfinished phrase"
        assert pages[2][0]["text"] == "continuation"


def test_unknown_image_count_is_a_hard_boundary():
    pages, analysis = stitch_page_paragraphs(
        {1: [paragraph(1, "unfinished phrase", (0.1, 0.72, 0.9, 0.94))],
         2: [paragraph(1, "continuation", (0.1, 0.04, 0.9, 0.22))]},
        {1: {"decision": "use_native"}, 2: context()}, total_pages=2,
    )
    assert analysis["stitched_count"] == 0


def test_stitches_continuation_spanning_three_pages():
    pages, analysis = stitch_page_paragraphs(
        {1: [paragraph(1, "the long paragraph begins", (0.1, 0.72, 0.9, 0.94))],
         2: [paragraph(1, "continues on the second page", (0.1, 0.04, 0.9, 0.94))],
         3: [paragraph(1, "and finishes on the third page.", (0.1, 0.04, 0.9, 0.22))]},
        {1: context(), 2: context(), 3: context()}, total_pages=3,
    )
    assert pages[1][0]["text"] == (
        "the long paragraph begins continues on the second page and finishes on the third page."
    )
    assert pages[2] == [] and pages[3] == []
    assert analysis["stitched_count"] == 2



def test_workspace_stitched_paragraph_checkpoint_roundtrip(tmp_path):
    from conversion.storage import JobWorkspace

    workspace = JobWorkspace(tmp_path / "stitching-job")
    workspace.create()
    pages = {
        1: [paragraph(1, "joined text", (0.1, 0.7, 0.9, 0.9))],
        2: [],
    }
    analysis = {
        "schema_version": 1,
        "total_pages": 2,
        "stitched_count": 1,
        "stitched": [{"from_page": 1, "to_page": 2, "paragraph_index": 1}],
    }
    workspace.save_stitched_paragraphs(pages, analysis)
    restored = workspace.load_stitched_paragraphs(2)
    assert restored == (pages, analysis)
    assert workspace.load_stitched_paragraphs(3) is None
