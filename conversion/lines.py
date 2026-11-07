"""Deterministic line-level reconstruction from page text blocks."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from .models import PageResult

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

def reconstruct_lines(result: PageResult) -> list[ReconstructedLine]:
    """Order line blocks vertically, then horizontally; do not infer paragraphs/columns."""
    blocks = [(i, block) for i, block in enumerate(result.blocks) if block.text.strip()]
    ordered = sorted(blocks, key=lambda item: (
        round((item[1].bbox[1] + item[1].bbox[3]) / 2, 3),
        item[1].bbox[0], item[0],
    ))
    return [
        ReconstructedLine(
            line_index=index, text=block.text.strip(), bbox=block.bbox,
            confidence=block.confidence, source=block.source.value,
            source_line_number=block.line_number,
            source_paragraph_number=block.paragraph_number,
        )
        for index, (_, block) in enumerate(ordered, start=1)
    ]
