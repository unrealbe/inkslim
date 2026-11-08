"""Conservative paragraph reconstruction from normalized page lines."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median

from .lines import ReconstructedLine
from .models import TextSource


PARAGRAPH_CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReconstructedParagraph:
    paragraph_index: int
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None
    source: str
    line_indices: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["bbox"] = list(self.bbox)
        value["line_indices"] = list(self.line_indices)
        return value


def _paragraph_break(
    previous: ReconstructedLine,
    current: ReconstructedLine,
    *,
    median_line_height: float,
    median_line_advance: float,
) -> bool:
    """Return true only for clear geometric or source-metadata boundaries."""
    if (
        previous.source_paragraph_number is not None
        and current.source_paragraph_number is not None
        and previous.source_paragraph_number != current.source_paragraph_number
    ):
        return True

    vertical_gap = current.bbox[1] - previous.bbox[3]
    previous_center = (previous.bbox[1] + previous.bbox[3]) / 2
    current_center = (current.bbox[1] + current.bbox[3]) / 2
    line_advance = current_center - previous_center

    # A larger-than-normal baseline step plus visible whitespace is a strong
    # paragraph signal. Requiring both avoids splitting ordinary loose leading.
    if (
        line_advance > median_line_advance * 1.55
        and vertical_gap > median_line_height * 0.35
    ):
        return True

    # A first-line indent is a useful secondary signal. The threshold is
    # deliberately conservative to ignore small glyph/measurement variation.
    indent_threshold = max(0.025, median_line_height * 0.75)
    if current.bbox[0] - previous.bbox[0] >= indent_threshold:
        return True

    return False


def reconstruct_paragraphs(
    lines: list[ReconstructedLine],
) -> list[ReconstructedParagraph]:
    """Group consecutive lines into paragraphs without dehyphenating or reordering."""
    if not lines:
        return []

    heights = [max(line.bbox[3] - line.bbox[1], 0.001) for line in lines]
    median_line_height = median(heights)
    advances = [
        (current.bbox[1] + current.bbox[3] - previous.bbox[1] - previous.bbox[3]) / 2
        for previous, current in zip(lines, lines[1:])
    ]
    positive_advances = [advance for advance in advances if advance > 0]
    median_line_advance = (
        median(positive_advances) if positive_advances else median_line_height * 1.3
    )

    groups: list[list[ReconstructedLine]] = []
    current_group = [lines[0]]
    for previous, current in zip(lines, lines[1:]):
        if _paragraph_break(
            previous,
            current,
            median_line_height=median_line_height,
            median_line_advance=median_line_advance,
        ):
            groups.append(current_group)
            current_group = [current]
        else:
            current_group.append(current)
    groups.append(current_group)

    paragraphs: list[ReconstructedParagraph] = []
    for index, group in enumerate(groups, start=1):
        sources = {line.source for line in group}
        source = next(iter(sources)) if len(sources) == 1 else TextSource.MIXED.value
        confidence_values = [
            line.confidence for line in group if line.confidence is not None
        ]
        paragraphs.append(
            ReconstructedParagraph(
                paragraph_index=index,
                text=" ".join(" ".join(line.text.split()) for line in group).strip(),
                bbox=(
                    min(line.bbox[0] for line in group),
                    min(line.bbox[1] for line in group),
                    max(line.bbox[2] for line in group),
                    max(line.bbox[3] for line in group),
                ),
                confidence=(
                    sum(confidence_values) / len(confidence_values)
                    if confidence_values else None
                ),
                source=source,
                line_indices=tuple(line.line_index for line in group),
            )
        )
    return paragraphs
