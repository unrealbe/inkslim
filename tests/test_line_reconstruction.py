from conversion.lines import reconstruct_lines
from conversion.models import PageResult, QualityStatus, TextBlock, TextSource


def make_result(blocks):
    return PageResult(
        page_number=1,
        width=600,
        height=800,
        source=TextSource.TESSERACT,
        quality=QualityStatus.HIGH,
        blocks=blocks,
    )


def block(text, bbox, line_number=None):
    return TextBlock(
        text, bbox, "text", 90, TextSource.TESSERACT, line_number, 1
    )


def test_reconstruct_lines_orders_single_column_by_vertical_then_horizontal_position():
    result = make_result([
        block("right", (0.6, 0.1, 0.9, 0.15), 1),
        block("lower", (0.1, 0.3, 0.5, 0.35), 2),
        block("left", (0.1, 0.1, 0.4, 0.15), 1),
    ])
    lines = reconstruct_lines(result)
    assert [line.text for line in lines] == ["left", "right", "lower"]
    assert [line.line_index for line in lines] == [1, 2, 3]
    assert lines[0].to_dict()["bbox"] == [0.1, 0.1, 0.4, 0.15]


def test_reconstruct_lines_ignores_empty_blocks_and_preserves_text():
    result = make_result([
        block("  ", (0, 0, 0, 0)),
        block("  Hallo   wereld  ", (0.1, 0.2, 0.8, 0.3)),
    ])
    lines = reconstruct_lines(result)
    assert len(lines) == 1
    assert lines[0].text == "Hallo   wereld"


def test_reconstruct_lines_reads_two_columns_down_left_then_down_right():
    result = make_result([
        block("L1", (0.08, 0.10, 0.43, 0.13), 1),
        block("R1", (0.57, 0.10, 0.92, 0.13), 2),
        block("L2", (0.08, 0.16, 0.43, 0.19), 3),
        block("R2", (0.57, 0.16, 0.92, 0.19), 4),
        block("L3", (0.08, 0.22, 0.43, 0.25), 5),
        block("R3", (0.57, 0.22, 0.92, 0.25), 6),
    ])
    assert [line.text for line in reconstruct_lines(result)] == [
        "L1", "L2", "L3", "R1", "R2", "R3"
    ]


def test_reconstruct_lines_keeps_full_width_heading_between_column_bands():
    result = make_result([
        block("L1", (0.08, 0.15, 0.43, 0.18), 1),
        block("R1", (0.57, 0.15, 0.92, 0.18), 2),
        block("L2", (0.08, 0.22, 0.43, 0.25), 3),
        block("R2", (0.57, 0.22, 0.92, 0.25), 4),
        block("Heading", (0.08, 0.30, 0.92, 0.34), 5),
        block("L3", (0.08, 0.40, 0.43, 0.43), 6),
        block("R3", (0.57, 0.40, 0.92, 0.43), 7),
        block("L4", (0.08, 0.47, 0.43, 0.50), 8),
        block("R4", (0.57, 0.47, 0.92, 0.50), 9),
    ])
    assert [line.text for line in reconstruct_lines(result)] == [
        "L1", "L2", "R1", "R2", "Heading", "L3", "L4", "R3", "R4"
    ]


def test_reconstruct_lines_does_not_infer_columns_without_vertical_overlap():
    result = make_result([
        block("Top left", (0.08, 0.05, 0.43, 0.08), 1),
        block("Top right", (0.57, 0.05, 0.92, 0.08), 2),
        block("Bottom left", (0.08, 0.70, 0.43, 0.73), 3),
        block("Bottom right", (0.57, 0.70, 0.92, 0.73), 4),
    ])
    # With only one line at each vertical level, the evidence is insufficient.
    assert [line.text for line in reconstruct_lines(result)] == [
        "Top left", "Top right", "Bottom left", "Bottom right"
    ]
