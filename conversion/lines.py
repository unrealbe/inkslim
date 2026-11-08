"""Deterministic line-level reconstruction with conservative column ordering."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median

from .models import PageResult, TextBlock, TextSource

# A gutter must be visible in normalized page coordinates. This deliberately
# avoids inferring columns from tiny alignment differences in a single column.
_MIN_GUTTER_WIDTH = 0.025
_MIN_LINES_PER_COLUMN = 2
_MIN_VERTICAL_OVERLAP = 0.25
_CROSSING_LINE_MARGIN = 0.012

# PDF text extractors may split a physical line at font changes, punctuation,
# ligatures, or marked-content boundaries. Build physical lines before reading
# order detection so tiny glyph baseline differences cannot reorder a sentence.
_MIN_BLOCK_VERTICAL_OVERLAP = 0.45
_MIN_CENTER_TOLERANCE = 0.004
_MAX_SAME_LINE_HORIZONTAL_GAP = 0.060

_APOSTROPHES = {"'", "’", "ʼ"}
_NO_SPACE_BEFORE = frozenset(".,;:!?%…)]}»”")
_NO_SPACE_AFTER = frozenset("([{«“")


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


def _block_center(block: TextBlock) -> float:
    return (block.bbox[1] + block.bbox[3]) / 2


def _block_height(block: TextBlock) -> float:
    return max(block.bbox[3] - block.bbox[1], 0.001)


def _horizontal_gap(left: TextBlock, right: TextBlock) -> float:
    return max(0.0, right.bbox[0] - left.bbox[2])


def _vertical_overlap_ratio(first: TextBlock, second: TextBlock) -> float:
    overlap = min(first.bbox[3], second.bbox[3]) - max(first.bbox[1], second.bbox[1])
    if overlap <= 0:
        return 0.0
    return overlap / min(_block_height(first), _block_height(second))


def _same_physical_line(
    block: TextBlock,
    members: list[tuple[int, TextBlock]],
    *,
    median_block_height: float,
) -> bool:
    """Return whether a text fragment belongs to an existing physical line.

    Vertical evidence establishes the line. A bounded horizontal gap prevents
    equally high text from separate columns being merged before gutter
    detection. Overlapping fragments are allowed because PDF glyph runs may
    overlap slightly at punctuation or font boundaries.
    """
    tolerance = max(_MIN_CENTER_TOLERANCE, median_block_height * 0.45)
    vertical_match = any(
        _vertical_overlap_ratio(block, member) >= _MIN_BLOCK_VERTICAL_OVERLAP
        or abs(_block_center(block) - _block_center(member)) <= tolerance
        for _, member in members
    )
    if not vertical_match:
        return False

    ordered = sorted((member for _, member in members), key=lambda item: item.bbox[0])
    line_left = ordered[0].bbox[0]
    line_right = max(member.bbox[2] for member in ordered)
    if block.bbox[2] < line_left:
        gap = line_left - block.bbox[2]
    elif block.bbox[0] > line_right:
        gap = block.bbox[0] - line_right
    else:
        gap = 0.0
    return gap <= _MAX_SAME_LINE_HORIZONTAL_GAP


def _estimated_character_width(block: TextBlock) -> float:
    visible = sum(not character.isspace() for character in block.text)
    return (block.bbox[2] - block.bbox[0]) / max(visible, 1)


def _needs_space(left: str, right: str, gap: float, character_width: float) -> bool:
    """Decide whether adjacent PDF fragments need an inserted space."""
    if not left or not right:
        return False
    if right[0] in _NO_SPACE_BEFORE or left[-1] in _NO_SPACE_AFTER:
        return False
    if right[0] in _APOSTROPHES or left[-1] in _APOSTROPHES:
        return False
    # Geometry remains the final signal for font-run splits within a word.
    return gap > max(character_width * 0.35, 0.0005)


def _merge_line_fragments(members: list[tuple[int, TextBlock]]) -> ReconstructedLine:
    ordered = sorted(members, key=lambda item: (item[1].bbox[0], item[0]))
    texts: list[str] = []
    previous: TextBlock | None = None
    previous_text = ""
    for _, block in ordered:
        text = block.text.strip()
        if not text:
            continue
        if previous is not None and texts:
            gap = _horizontal_gap(previous, block)
            character_width = median(
                (_estimated_character_width(previous), _estimated_character_width(block))
            )
            if _needs_space(previous_text, text, gap, character_width):
                texts.append(" ")
        texts.append(text)
        previous = block
        previous_text = text

    boxes = [block.bbox for _, block in ordered]
    confidence_values = [
        block.confidence for _, block in ordered if block.confidence is not None
    ]
    sources = {block.source for _, block in ordered}
    line_numbers = {block.line_number for _, block in ordered if block.line_number is not None}
    paragraph_numbers = {
        block.paragraph_number
        for _, block in ordered
        if block.paragraph_number is not None
    }
    return ReconstructedLine(
        line_index=0,
        text="".join(texts),
        bbox=(
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ),
        confidence=(
            sum(confidence_values) / len(confidence_values)
            if confidence_values
            else None
        ),
        source=(
            next(iter(sources)).value
            if len(sources) == 1
            else TextSource.MIXED.value
        ),
        source_line_number=(next(iter(line_numbers)) if len(line_numbers) == 1 else None),
        source_paragraph_number=(
            next(iter(paragraph_numbers)) if len(paragraph_numbers) == 1 else None
        ),
    )


def _cluster_physical_lines(
    blocks: list[tuple[int, TextBlock]],
) -> list[tuple[int, ReconstructedLine]]:
    if not blocks:
        return []
    median_block_height = median(_block_height(block) for _, block in blocks)
    # Coarse vertical ordering only selects candidate lines. Fragment ordering
    # inside a confirmed physical line is always horizontal and stable.
    pending = sorted(
        blocks,
        key=lambda item: (round(_block_center(item[1]), 3), item[1].bbox[0], item[0]),
    )
    groups: list[list[tuple[int, TextBlock]]] = []
    for item in pending:
        block = item[1]
        candidates = [
            group
            for group in groups
            if _same_physical_line(
                block, group, median_block_height=median_block_height
            )
        ]
        if not candidates:
            groups.append([item])
            continue
        # Prefer the closest baseline when more than one nearby line qualifies.
        target = min(
            candidates,
            key=lambda group: abs(
                _block_center(block)
                - median(_block_center(member) for _, member in group)
            ),
        )
        target.append(item)

    # A raised apostrophe or font run can be encountered before the longer
    # baseline fragment that connects two provisional groups. Coalesce such
    # groups after the first pass instead of letting iteration order decide.
    changed = True
    while changed:
        changed = False
        for left_index in range(len(groups)):
            for right_index in range(left_index + 1, len(groups)):
                if any(
                    _same_physical_line(
                        block, groups[left_index],
                        median_block_height=median_block_height,
                    )
                    for _, block in groups[right_index]
                ):
                    groups[left_index].extend(groups.pop(right_index))
                    changed = True
                    break
            if changed:
                break

    return [
        (min(index for index, _ in group), _merge_line_fragments(group))
        for group in groups
    ]


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
    """Find a clear, vertically supported gutter between two text columns."""
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
                line
                for _, line in lines
                if line.bbox[2] <= gutter - _CROSSING_LINE_MARGIN
            ]
            right = [
                line
                for _, line in lines
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
            overlap = min(max(left_centers), max(right_centers)) - max(
                min(left_centers), min(right_centers)
            )
            overlap_ratio = overlap / min(left_span, right_span)
            if overlap_ratio < _MIN_VERTICAL_OVERLAP:
                continue
            support = min(len(left), len(right))
            candidates.append((gap * support, support, gutter))
    if not candidates:
        return None
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
            left.append(item)
    return _stable_vertical_order(left) + _stable_vertical_order(right)


def _order_two_columns(
    lines: list[tuple[int, ReconstructedLine]],
    gutter: float,
) -> list[tuple[int, ReconstructedLine]]:
    anchors = _stable_vertical_order(
        [
            item
            for item in lines
            if (
                item[1].bbox[0] < gutter - _CROSSING_LINE_MARGIN
                and item[1].bbox[2] > gutter + _CROSSING_LINE_MARGIN
            )
        ]
    )
    if not anchors:
        return _order_column_band(lines, gutter)
    ordered: list[tuple[int, ReconstructedLine]] = []
    remaining = list(lines)
    for anchor in anchors:
        anchor_center = _vertical_center(anchor[1])
        before = [
            item
            for item in remaining
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
    """Build physical lines, then apply conservative page reading order."""
    blocks = [
        (index, block)
        for index, block in enumerate(result.blocks)
        if block.text.strip()
    ]
    physical_lines = _cluster_physical_lines(blocks)
    gutter = _detect_two_column_gutter(physical_lines)
    ordered = (
        _order_two_columns(physical_lines, gutter)
        if gutter is not None
        else _stable_vertical_order(physical_lines)
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
