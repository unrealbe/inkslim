"""Serializable per-page PDF analysis result."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any

from ..models import UnsupportedCheckpointVersionError

ANALYSIS_SCHEMA_VERSION = 1


class PageDecision(StrEnum):
    USE_NATIVE = "use_native"
    OCR_REQUIRED = "ocr_required"
    EMPTY = "empty"


def _finite(value: float, label: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _ratio(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    result = _finite(value, label)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return result


@dataclass(slots=True)
class PageAnalysis:
    page_number: int
    width: float
    height: float
    character_count: int
    word_count: int
    image_count: int
    has_native_text: bool
    native_text_quality: float | None
    decision: PageDecision
    meaningful_character_ratio: float | None = None
    control_character_ratio: float | None = None
    replacement_character_ratio: float | None = None
    single_character_word_ratio: float | None = None
    geometry_usable_ratio: float | None = None
    meaningful_character_count: int = 0
    render_dpi: int | None = None
    render_width_px: int | None = None
    render_height_px: int | None = None
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number must be at least 1")
        self.width = _finite(self.width, "width")
        self.height = _finite(self.height, "height")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("page dimensions must be positive")
        for name in ("character_count", "word_count", "image_count", "meaningful_character_count"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.native_text_quality is not None:
            self.native_text_quality = _ratio(self.native_text_quality, "native_text_quality")
        for name in (
            "meaningful_character_ratio",
            "control_character_ratio",
            "replacement_character_ratio",
            "single_character_word_ratio",
            "geometry_usable_ratio",
        ):
            setattr(self, name, _ratio(getattr(self, name), name))
        if self.render_dpi is not None and self.render_dpi < 1:
            raise ValueError("render_dpi must be positive")
        for name in ("render_width_px", "render_height_px"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive")
        self.decision = PageDecision(self.decision)
        self.warnings = [str(warning) for warning in self.warnings]

    @property
    def requires_ocr(self) -> bool:
        return self.decision is PageDecision.OCR_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = ANALYSIS_SCHEMA_VERSION
        value["decision"] = self.decision.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PageAnalysis:
        version = value.get("schema_version", 0)
        if type(version) is not int:
            raise UnsupportedCheckpointVersionError("Invalid PageAnalysis schema version")
        if version == 0:
            # Initial analysis checkpoints did not persist derived render dimensions.
            value = dict(value)
            value["schema_version"] = ANALYSIS_SCHEMA_VERSION
            value.setdefault("warnings", [])
            value.setdefault("render_dpi", None)
            value.setdefault("render_width_px", None)
            value.setdefault("render_height_px", None)
        elif version != ANALYSIS_SCHEMA_VERSION:
            raise UnsupportedCheckpointVersionError(
                f"Unsupported PageAnalysis schema version: {version}"
            )
        return cls(
            page_number=int(value["page_number"]),
            width=float(value["width"]),
            height=float(value["height"]),
            character_count=int(value["character_count"]),
            word_count=int(value["word_count"]),
            image_count=int(value["image_count"]),
            has_native_text=bool(value["has_native_text"]),
            native_text_quality=value.get("native_text_quality"),
            decision=PageDecision(value["decision"]),
            meaningful_character_ratio=value.get("meaningful_character_ratio"),
            control_character_ratio=value.get("control_character_ratio"),
            replacement_character_ratio=value.get("replacement_character_ratio"),
            single_character_word_ratio=value.get("single_character_word_ratio"),
            geometry_usable_ratio=value.get("geometry_usable_ratio"),
            meaningful_character_count=int(value.get("meaningful_character_count", 0)),
            render_dpi=value.get("render_dpi"),
            render_width_px=value.get("render_width_px"),
            render_height_px=value.get("render_height_px"),
            warnings=list(value.get("warnings", [])),
        )
