"""PDFium page rendering with pre-allocation pixel and dimension limits."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RenderPlanError(ValueError):
    """A PDF page cannot be rendered within configured safety limits."""


@dataclass(frozen=True, slots=True)
class PDFRenderLimits:
    dpi: int = 300
    minimum_dpi: int = 120
    max_pixels: int = 50_000_000
    max_dimension: int = 12_000

    def __post_init__(self) -> None:
        if self.dpi < 72 or self.minimum_dpi < 72:
            raise ValueError("Render DPI values must be at least 72")
        if self.minimum_dpi > self.dpi:
            raise ValueError("minimum_dpi cannot exceed dpi")
        if self.max_pixels < 1 or self.max_dimension < 1:
            raise ValueError("Render size limits must be positive")


@dataclass(frozen=True, slots=True)
class RenderPlan:
    requested_dpi: int
    actual_dpi: int
    width_px: int
    height_px: int
    warnings: tuple[str, ...] = ()


def build_render_plan(
    width_points: float,
    height_points: float,
    *,
    rotation: int = 0,
    limits: PDFRenderLimits | None = None,
) -> RenderPlan:
    limits = limits or PDFRenderLimits()
    if not math.isfinite(width_points) or not math.isfinite(height_points):
        raise RenderPlanError("Page dimensions are not finite")
    if width_points <= 0 or height_points <= 0:
        raise RenderPlanError("Page dimensions must be positive")
    if rotation not in {0, 90, 180, 270}:
        raise RenderPlanError("Unsupported PDF page rotation")

    if rotation in {90, 270}:
        width_points, height_points = height_points, width_points

    for dpi in range(limits.dpi, limits.minimum_dpi - 1, -1):
        width_px = max(1, math.ceil(width_points * dpi / 72.0))
        height_px = max(1, math.ceil(height_points * dpi / 72.0))
        if (
            width_px <= limits.max_dimension
            and height_px <= limits.max_dimension
            and width_px * height_px <= limits.max_pixels
        ):
            warnings = ()
            if dpi < limits.dpi:
                warnings = (f"RENDER_DPI_REDUCED:{limits.dpi}->{dpi}",)
            return RenderPlan(limits.dpi, dpi, width_px, height_px, warnings)

    raise RenderPlanError(
        f"Page cannot be rendered at the minimum allowed DPI ({limits.minimum_dpi}) "
        f"within {limits.max_pixels:,} pixels and {limits.max_dimension:,} pixels per side"
    )


def render_pdf_page(
    page: Any,
    destination: Path,
    *,
    limits: PDFRenderLimits | None = None,
    plan: RenderPlan | None = None,
) -> RenderPlan:
    """Render one page to PNG, verifying the resulting dimensions before committing it."""
    limits = limits or PDFRenderLimits()
    width_points, height_points = page.get_size()
    page_rotation = int(page.get_rotation())
    plan = plan or build_render_plan(
        width_points,
        height_points,
        rotation=page_rotation,
        limits=limits,
    )

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    bitmap = None
    image = None
    try:
        bitmap = page.render(
            scale=plan.actual_dpi / 72.0,
            fill_color=(255, 255, 255, 255),
            grayscale=False,
            limit_image_cache=True,
        )
        image = bitmap.to_pil()
        width_px, height_px = image.size
        if (
            width_px > limits.max_dimension
            or height_px > limits.max_dimension
            or width_px * height_px > limits.max_pixels
        ):
            raise RenderPlanError("PDFium rendered dimensions exceeded the planned safety limits")
        image.save(temporary, format="PNG", optimize=True)
        image.close()
        image = None
        os.replace(temporary, destination)
        return plan
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if image is not None:
            image.close()
        if bitmap is not None:
            bitmap.close()
