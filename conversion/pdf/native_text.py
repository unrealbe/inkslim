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
