"""Native PDF text extraction and lightweight geometry sampling."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NativeTextExtraction:
    text: str
    character_count: int
    geometry_usable_ratio: float | None


def _geometry_ratio(
    text_page: Any,
    character_count: int,
    page_bbox: tuple[float, float, float, float],
    *,
    max_samples: int = 160,
) -> float | None:
    if character_count <= 0:
        return None
    sample_count = min(character_count, max_samples)
    if sample_count == 1:
        indices = [0]
    else:
        indices = sorted(
            {round(index * (character_count - 1) / (sample_count - 1))
             for index in range(sample_count)}
        )

    left_bound, bottom_bound, right_bound, top_bound = page_bbox
    margin_x = max(1.0, (right_bound - left_bound) * 0.05)
    margin_y = max(1.0, (top_bound - bottom_bound) * 0.05)
    valid = 0
    sampled = 0

    for index in indices:
        sampled += 1
        try:
            left, bottom, right, top = text_page.get_charbox(index)
            values = (left, bottom, right, top)
            if not all(math.isfinite(float(value)) for value in values):
                continue
            if left > right or bottom > top:
                continue
            if (
                left >= left_bound - margin_x
                and right <= right_bound + margin_x
                and bottom >= bottom_bound - margin_y
                and top <= top_bound + margin_y
            ):
                valid += 1
        except Exception:
            continue

    return valid / sampled if sampled else None


def extract_native_text(page: Any) -> NativeTextExtraction:
    """Extract full-Unicode text and sample glyph boxes without storing every glyph."""
    width, height = page.get_size()
    page_bbox = tuple(float(value) for value in page.get_bbox())
    if len(page_bbox) != 4 or width <= 0 or height <= 0:
        raise ValueError("PDF page has invalid dimensions")

    text_page = page.get_textpage()
    try:
        text = text_page.get_text_bounded(errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        character_count = int(text_page.count_chars())
        geometry_ratio = _geometry_ratio(
            text_page,
            character_count,
            page_bbox,  # PDFium coordinates use the page's canvas coordinate system.
        )
    finally:
        text_page.close()

    return NativeTextExtraction(
        text=text,
        character_count=character_count,
        geometry_usable_ratio=geometry_ratio,
    )



class NativeTextGeometryError(RuntimeError):
    """Native text could not be mapped reliably to page geometry."""


def extract_native_page_result(
    page: Any,
    page_number: int,
    *,
    native_text_score: float | None = None,
):
    """Build the shared PageResult contract from PDFium text and glyph geometry.

    Text is grouped into visual lines using explicit line breaks and glyph baselines.
    Bounding boxes are normalized to a top-left-origin page coordinate system.
    """
    from statistics import median

    from ..models import PageMetrics, PageResult, QualityStatus, TextBlock, TextSource

    if page_number < 1:
        raise ValueError("page_number must be at least 1")

    text_page = None
    try:
        width, height = (float(value) for value in page.get_size())
        left_bound, bottom_bound, right_bound, top_bound = (
            float(value) for value in page.get_bbox()
        )
        page_width = right_bound - left_bound
        page_height = top_bound - bottom_bound
        if (
            width <= 0 or height <= 0 or page_width <= 0 or page_height <= 0
            or not all(math.isfinite(value) for value in (
                width, height, left_bound, bottom_bound, right_bound, top_bound
            ))
        ):
            raise NativeTextGeometryError("PDF page has invalid text geometry")

        text_page = page.get_textpage()
        character_count = int(text_page.count_chars())
        lines: list[dict[str, Any]] = []
        current_text: list[str] = []
        current_boxes: list[tuple[float, float, float, float]] = []
        current_centers: list[float] = []
        current_heights: list[float] = []
        non_whitespace = 0
        geometry_valid = 0

        def flush_line() -> None:
            nonlocal current_text, current_boxes, current_centers, current_heights
            value = "".join(current_text).strip()
            if value and current_boxes:
                lines.append({
                    "text": value,
                    "bbox": (
                        min(box[0] for box in current_boxes),
                        min(box[1] for box in current_boxes),
                        max(box[2] for box in current_boxes),
                        max(box[3] for box in current_boxes),
                    ),
                })
            current_text = []
            current_boxes = []
            current_centers = []
            current_heights = []

        for index in range(character_count):
            character = text_page.get_text_range(index, 1, errors="replace")
            if not character:
                continue
            if character in {"\\r", "\\n", "\\r\\n"}:
                flush_line()
                continue

            # Preserve spaces in the line text. PDFium may give spaces zero-area
            # boxes, so only visible glyphs contribute to line geometry.
            if character.isspace():
                if current_text:
                    current_text.append(character)
                continue

            non_whitespace += 1
            try:
                char_left, char_bottom, char_right, char_top = (
                    float(value) for value in text_page.get_charbox(index)
                )
                valid = (
                    all(math.isfinite(value) for value in (
                        char_left, char_bottom, char_right, char_top
                    ))
                    and char_left < char_right
                    and char_bottom < char_top
                    and char_left >= left_bound - page_width * 0.05
                    and char_right <= right_bound + page_width * 0.05
                    and char_bottom >= bottom_bound - page_height * 0.05
                    and char_top <= top_bound + page_height * 0.05
                )
            except Exception:
                valid = False

            if not valid:
                # Keep the source text intact, but fail closed rather than emit
                # misleading geometry for a page classified as native-quality.
                raise NativeTextGeometryError(
                    f"PDFium did not provide usable geometry for character {index}"
                )

            geometry_valid += 1
            x0 = min(1.0, max(0.0, (char_left - left_bound) / page_width))
            x1 = min(1.0, max(0.0, (char_right - left_bound) / page_width))
            y0 = min(1.0, max(0.0, (top_bound - char_top) / page_height))
            y1 = min(1.0, max(0.0, (top_bound - char_bottom) / page_height))
            bbox = (x0, y0, x1, y1)
            center = (y0 + y1) / 2.0
            glyph_height = y1 - y0

            if current_boxes:
                reference_center = median(current_centers)
                reference_height = median(current_heights)
                tolerance = max(reference_height * 0.65, glyph_height * 0.65, 0.0025)
                if abs(center - reference_center) > tolerance:
                    flush_line()

            current_text.append(character)
            current_boxes.append(bbox)
            current_centers.append(center)
            current_heights.append(glyph_height)

        flush_line()

        if non_whitespace == 0 or not lines:
            raise NativeTextGeometryError("Native text page contains no usable glyphs")
        if geometry_valid / non_whitespace < 0.80:
            raise NativeTextGeometryError("Native text geometry coverage is insufficient")

        blocks = [
            TextBlock(
                text=line["text"],
                bbox=line["bbox"],
                block_type="text",
                confidence=None,
                source=TextSource.NATIVE,
                line_number=line_number,
            )
            for line_number, line in enumerate(lines, start=1)
        ]
        return PageResult(
            page_number=page_number,
            width=width,
            height=height,
            blocks=blocks,
            source=TextSource.NATIVE,
            quality=QualityStatus.HIGH,
            metrics=PageMetrics(native_text_score=native_text_score),
        )
    except NativeTextGeometryError:
        raise
    except Exception as exc:
        raise NativeTextGeometryError("Native PDF text geometry extraction failed") from exc
    finally:
        if text_page is not None:
            text_page.close()
