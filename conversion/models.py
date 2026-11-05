"""Engine-independent data models shared by conversion stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any


CHECKPOINT_SCHEMA_VERSION = 1


class UnsupportedCheckpointVersionError(ValueError):
    """Raised when a checkpoint was written by an incompatible future schema."""



class TextSource(StrEnum):
    NATIVE = "native"
    TESSERACT = "tesseract"
    MIXED = "mixed"


class QualityStatus(StrEnum):
    HIGH = "high"
    REVIEW = "review"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    EXTRACTING = "extracting"
    OCR = "ocr"
    POSTPROCESSING = "postprocessing"
    BUILDING_EPUB = "building_epub"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = frozenset(
    {
        JobStatus.ANALYZING,
        JobStatus.EXTRACTING,
        JobStatus.OCR,
        JobStatus.POSTPROCESSING,
        JobStatus.BUILDING_EPUB,
        JobStatus.VALIDATING,
    }
)
TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)


def _finite_number(value: float, label: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _optional_ratio(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    number = _finite_number(value, label)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return number


def _optional_score(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    number = _finite_number(value, label)
    if not 0.0 <= number <= 100.0:
        raise ValueError(f"{label} must be between 0 and 100")
    return number


@dataclass(slots=True)
class TextBlock:
    """A text block with a normalized (0..1) page-relative bounding box."""

    text: str
    bbox: tuple[float, float, float, float]
    block_type: str
    confidence: float | None
    source: TextSource
    line_number: int | None = None
    paragraph_number: int | None = None

    def __post_init__(self) -> None:
        if len(self.bbox) != 4:
            raise ValueError("bbox must contain x0, y0, x1 and y1")
        coordinates = tuple(_finite_number(value, "bbox coordinate") for value in self.bbox)
        x0, y0, x1, y1 = coordinates
        if not (0.0 <= x0 <= x1 <= 1.0 and 0.0 <= y0 <= y1 <= 1.0):
            raise ValueError("bbox coordinates must be ordered and normalized between 0 and 1")
        self.bbox = coordinates
        self.source = TextSource(self.source)
        if self.confidence is not None:
            self.confidence = _optional_score(self.confidence, "confidence")
        if self.line_number is not None and self.line_number < 0:
            raise ValueError("line_number cannot be negative")
        if self.paragraph_number is not None and self.paragraph_number < 0:
            raise ValueError("paragraph_number cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source"] = self.source.value
        value["bbox"] = list(self.bbox)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TextBlock:
        return cls(
            text=str(value["text"]),
            bbox=tuple(value["bbox"]),  # type: ignore[arg-type]
            block_type=str(value.get("block_type", "text")),
            confidence=value.get("confidence"),
            source=TextSource(value["source"]),
            line_number=value.get("line_number"),
            paragraph_number=value.get("paragraph_number"),
        )


@dataclass(slots=True)
class PageMetrics:
    native_text_score: float | None = None
    mean_ocr_confidence: float | None = None
    meaningful_character_ratio: float | None = None
    suspicious_character_ratio: float | None = None
    single_character_word_ratio: float | None = None

    def __post_init__(self) -> None:
        self.native_text_score = _optional_score(self.native_text_score, "native_text_score")
        self.mean_ocr_confidence = _optional_score(
            self.mean_ocr_confidence, "mean_ocr_confidence"
        )
        self.meaningful_character_ratio = _optional_ratio(
            self.meaningful_character_ratio, "meaningful_character_ratio"
        )
        self.suspicious_character_ratio = _optional_ratio(
            self.suspicious_character_ratio, "suspicious_character_ratio"
        )
        self.single_character_word_ratio = _optional_ratio(
            self.single_character_word_ratio, "single_character_word_ratio"
        )

    def to_dict(self) -> dict[str, float | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PageMetrics:
        return cls(**{key: value.get(key) for key in cls.__dataclass_fields__})


@dataclass(slots=True)
class PageResult:
    page_number: int
    width: float
    height: float
    blocks: list[TextBlock]
    source: TextSource
    quality: QualityStatus
    metrics: PageMetrics = field(default_factory=PageMetrics)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number must be at least 1")
        self.width = _finite_number(self.width, "width")
        self.height = _finite_number(self.height, "height")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("page dimensions must be positive")
        self.source = TextSource(self.source)
        self.quality = QualityStatus(self.quality)
        if not isinstance(self.metrics, PageMetrics):
            self.metrics = PageMetrics.from_dict(self.metrics)
        if not all(isinstance(block, TextBlock) for block in self.blocks):
            raise TypeError("blocks must contain TextBlock instances")
        self.warnings = [str(item) for item in self.warnings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "blocks": [block.to_dict() for block in self.blocks],
            "source": self.source.value,
            "quality": self.quality.value,
            "metrics": self.metrics.to_dict(),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PageResult:
        version = value.get("schema_version", 0)
        if type(version) is not int:
            raise UnsupportedCheckpointVersionError("Invalid PageResult schema version")
        if version == 0:
            # Version 0 was the initial unversioned checkpoint shape. Its fields match
            # version 1; the metrics and warnings fields are optional.
            value = dict(value)
            value["schema_version"] = CHECKPOINT_SCHEMA_VERSION
            value.setdefault("metrics", {})
            value.setdefault("warnings", [])
        elif version != CHECKPOINT_SCHEMA_VERSION:
            raise UnsupportedCheckpointVersionError(
                f"Unsupported PageResult schema version: {version}"
            )
        return cls(
            page_number=int(value["page_number"]),
            width=float(value["width"]),
            height=float(value["height"]),
            blocks=[TextBlock.from_dict(block) for block in value.get("blocks", [])],
            source=TextSource(value["source"]),
            quality=QualityStatus(value["quality"]),
            metrics=PageMetrics.from_dict(value.get("metrics", {})),
            warnings=list(value.get("warnings", [])),
        )


@dataclass(slots=True)
class Job:
    id: str
    status: JobStatus
    input_name: str
    input_path: str
    language: str
    output_path: str | None = None
    report_path: str | None = None
    total_pages: int | None = None
    completed_pages: int = 0
    current_page: int | None = None
    progress_message: str | None = None
    cancellation_requested: bool = False
    worker_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> Job:
        return cls(
            id=row["id"],
            status=JobStatus(row["status"]),
            input_name=row["input_name"],
            input_path=row["input_path"],
            language=row["language"],
            output_path=row["output_path"],
            report_path=row["report_path"],
            total_pages=row["total_pages"],
            completed_pages=row["completed_pages"],
            current_page=row["current_page"],
            progress_message=row["progress_message"],
            cancellation_requested=bool(row["cancellation_requested"]),
            worker_id=row["worker_id"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            expires_at=row["expires_at"],
        )
