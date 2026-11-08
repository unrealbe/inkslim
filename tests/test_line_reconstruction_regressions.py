from conversion.lines import reconstruct_lines
from conversion.models import PageResult, QualityStatus, TextBlock, TextSource


def make_result(blocks, source=TextSource.NATIVE):
    return PageResult(
        page_number=1,
        width=600,
        height=800,
        source=source,
        quality=QualityStatus.HIGH,
        blocks=blocks,
    )


def block(text, bbox, *, source=TextSource.NATIVE, confidence=None, line=1, paragraph=1):
    return TextBlock(text, bbox, "text", confidence, source, line, paragraph)


def test_fragments_on_same_physical_line_are_merged_left_to_right():
    result = make_result([
        block("right", (0.24, 0.101, 0.34, 0.151)),
        block("lower", (0.10, 0.30, 0.25, 0.35), line=2),
        block("left", (0.10, 0.10, 0.20, 0.15)),
    ])

    lines = reconstruct_lines(result)

    assert [line.text for line in lines] == ["left right", "lower"]
    assert [line.line_index for line in lines] == [1, 2]
    assert lines[0].to_dict()["bbox"] == [0.1, 0.1, 0.34, 0.151]


def test_tiny_baseline_differences_do_not_reorder_apostrophe_fragments():
    result = make_result([
        block("There is a scent, that", (0.10, 0.1005, 0.55, 0.1210)),
        block("’", (0.551, 0.0988, 0.558, 0.1195)),
        block("s all.", (0.559, 0.0992, 0.64, 0.1200)),
    ])

    lines = reconstruct_lines(result)

    assert [line.text for line in lines] == ["There is a scent, that’s all."]


def test_possessives_contractions_and_punctuation_keep_correct_spacing():
    result = make_result([
        block("A canine", (0.10, 0.10, 0.22, 0.13)),
        block("’", (0.221, 0.099, 0.226, 0.129)),
        block("s ears", (0.227, 0.10, 0.31, 0.13)),
        block(",", (0.311, 0.10, 0.316, 0.13)),
        block("it", (0.34, 0.10, 0.37, 0.13)),
        block("’", (0.371, 0.099, 0.376, 0.129)),
        block("s listening", (0.377, 0.10, 0.49, 0.13)),
        block(".", (0.491, 0.10, 0.496, 0.13)),
    ])

    lines = reconstruct_lines(result)

    assert [line.text for line in lines] == ["A canine’s ears, it’s listening."]


def test_same_height_fragments_in_separate_columns_are_not_merged():
    result = make_result([
        block("left one", (0.08, 0.10, 0.34, 0.14), line=1),
        block("right one", (0.62, 0.10, 0.90, 0.14), line=1),
        block("left two", (0.08, 0.18, 0.34, 0.22), line=2),
        block("right two", (0.62, 0.18, 0.90, 0.22), line=2),
    ])

    lines = reconstruct_lines(result)

    assert [line.text for line in lines] == [
        "left one",
        "left two",
        "right one",
        "right two",
    ]


def test_single_column_short_fragments_do_not_trigger_false_columns():
    result = make_result([
        block("There is a scent,", (0.10, 0.10, 0.42, 0.14), line=1),
        block("that’s all.", (0.10, 0.16, 0.29, 0.20), line=2),
        block("Or not a scent,", (0.10, 0.22, 0.38, 0.26), line=3),
        block("not in the killing void of space.", (0.10, 0.28, 0.64, 0.32), line=4),
    ])

    lines = reconstruct_lines(result)

    assert [line.text for line in lines] == [
        "There is a scent,",
        "that’s all.",
        "Or not a scent,",
        "not in the killing void of space.",
    ]


def test_empty_blocks_are_ignored_and_internal_text_is_preserved():
    result = make_result(
        [
            block("  ", (0, 0, 0, 0)),
            block("  Hallo   wereld  ", (0.1, 0.2, 0.8, 0.3)),
        ],
        source=TextSource.TESSERACT,
    )

    lines = reconstruct_lines(result)

    assert len(lines) == 1
    assert lines[0].text == "Hallo   wereld"
