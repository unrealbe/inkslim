"""Deterministic line-level reconstruction with conservative column ordering."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import PageResult


# A gutter must be visible in normalized page coordinates. This deliberately
# avoids inferring columns from tiny alignment differences in a single column.
_MIN_GUTTER_WIDTH = 0.025
_MIN_LINES_PER_COLUMN = 2
_MIN_VERTICAL_OVERLAP = 0.25
_CROSSING_LINE_MARGIN = 0.012


@dataclass(frozen=True, slots=True)
class ReconstructedLine:
    line_index: int
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None
    source: str
    source_line_number: int | None
    source_paragraph_number: int | None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["bbox"] = list(self.bbox)
        return value


def _vertical_center(line: ReconstructedLine) -> float:
    return (line.bbox[1] + line.bbox[3]) / 2


def _stable_vertical_order(
    lines: list[tuple[int, ReconstructedLine]],
) -> list[tuple[int, ReconstructedLine]]:
    return sorted(
        lines,
        key=lambda item: (
            round(_vertical_center(item[1]), 4),
            item[1].bbox[0],
            item[0],
        ),
    )


def _detect_two_column_gutter(
    lines: list[tuple[int, ReconstructedLine]],
) -> float | None:
    """Find a clear, vertically supported gutter between two text columns.

    Wide lines crossing the gutter are allowed as full-width headings/anchors.
    A candidate is rejected unless both columns contain multiple lines and
    their vertical ranges overlap, which avoids treating side notes or
    sequential blocks as a two-column page.
    """
    if len(lines) < _MIN_LINES_PER_COLUMN * 2:
        return None

    left_edges = sorted({line.bbox[2] for _, line in lines})
    right_edges = sorted({line.bbox[0] for _, line in lines})
    candidates: list[tuple[float, int, float]] = []

    for left_edge in left_edges:
        for right_edge in right_edges:
            gap = right_edge - left_edge
            if gap < _MIN_GUTTER_WIDTH:
                continue
            gutter = (left_edge + right_edge) / 2
            left = [
                line for _, line in lines
                if line.bbox[2] <= gutter - _CROSSING_LINE_MARGIN
            ]
            right = [
                line for _, line in lines
                if line.bbox[0] >= gutter + _CROSSING_LINE_MARGIN
            ]
            if (
                len(left) < _MIN_LINES_PER_COLUMN
                or len(right) < _MIN_LINES_PER_COLUMN
            ):
                continue

            left_centers = [_vertical_center(line) for line in left]
            right_centers = [_vertical_center(line) for line in right]
            left_span = max(left_centers) - min(left_centers)
            right_span = max(right_centers) - min(right_centers)
            if left_span <= 0 or right_span <= 0:
                continue
            overlap = (
                min(max(left_centers), max(right_centers))
                - max(min(left_centers), min(right_centers))
            )
            overlap_ratio = overlap / min(left_span, right_span)
            if overlap_ratio < _MIN_VERTICAL_OVERLAP:
                continue

            support = min(len(left), len(right))
            candidates.append((gap * support, support, gutter))

    if not candidates:
        return None
    # Prefer a broad gutter with balanced evidence; deterministic tie-break.
    return max(candidates, key=lambda item: (item[0], item[1], -item[2]))[2]


def _order_column_band(
    lines: list[tuple[int, ReconstructedLine]],
    gutter: float,
) -> list[tuple[int, ReconstructedLine]]:
    left: list[tuple[int, ReconstructedLine]] = []
    right: list[tuple[int, ReconstructedLine]] = []
    for item in lines:
        line = item[1]
        if line.bbox[2] <= gutter:
            left.append(item)
        elif line.bbox[0] >= gutter:
            right.append(item)
        else:
            # Ambiguous lines are kept in stable vertical order rather than
            # being silently assigned to a column.
            left.append(item)
    return _stable_vertical_order(left) + _stable_vertical_order(right)


def _order_two_columns(
    lines: list[tuple[int, ReconstructedLine]],
    gutter: float,
) -> list[tuple[int, ReconstructedLine]]:
    anchors = _stable_vertical_order([
        item for item in lines
        if (
            item[1].bbox[0] < gutter - _CROSSING_LINE_MARGIN
            and item[1].bbox[2] > gutter + _CROSSING_LINE_MARGIN
        )
    ])
    if not anchors:
        return _order_column_band(lines, gutter)

    ordered: list[tuple[int, ReconstructedLine]] = []
    remaining = list(lines)
    for anchor in anchors:
        anchor_center = _vertical_center(anchor[1])
        before = [
            item for item in remaining
            if item[0] != anchor[0] and _vertical_center(item[1]) < anchor_center
        ]
        ordered.extend(_order_column_band(before, gutter))
        ordered.append(anchor)
        consumed = {item[0] for item in before}
        consumed.add(anchor[0])
        remaining = [item for item in remaining if item[0] not in consumed]
    ordered.extend(_order_column_band(remaining, gutter))
    return ordered


def reconstruct_lines(result: PageResult) -> list[ReconstructedLine]:
    """Reconstruct reading order, using a two-column model only with clear evidence.

    Single-column pages retain top-to-bottom/left-to-right ordering. For a
    detected two-column page, each band is read down the left column, then
    down the right column. Full-width lines act as boundaries between bands.
    """
    blocks = [
        (index, block)
        for index, block in enumerate(result.blocks)
        if block.text.strip()
    ]
    lines = [
        ReconstructedLine(
            line_index=0,
            text=block.text.strip(),
            bbox=block.bbox,
            confidence=block.confidence,
            source=block.source.value,
            source_line_number=block.line_number,
            source_paragraph_number=block.paragraph_number,
        )
        for _, block in blocks
    ]
    indexed = list(zip((index for index, _ in blocks), lines))
    gutter = _detect_two_column_gutter(indexed)
    ordered = (
        _order_two_columns(indexed, gutter)
        if gutter is not None
        else _stable_vertical_order(indexed)
    )
    return [
        ReconstructedLine(
            line_index=index,
            text=line.text,
            bbox=line.bbox,
            confidence=line.confidence,
            source=line.source,
            source_line_number=line.source_line_number,
            source_paragraph_number=line.source_paragraph_number,
        )
        for index, (_, line) in enumerate(ordered, start=1)
    ]
