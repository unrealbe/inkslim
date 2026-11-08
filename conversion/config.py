"""Configuration for the local conversion worker and persistent job storage."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ConversionConfig:
    data_dir: Path
    max_ocr_pages: int = 0
    ocr_workers: int = 1
    ocr_timeout_page: int = 120
    job_retention_hours: int = 24
    max_conversion_storage_mb: int = 5120
    queue_poll_seconds: float = 2.0
    pdf_render_dpi: int = 300
    min_render_dpi: int = 120
    max_render_pixels: int = 50_000_000
    max_render_dimension: int = 12_000
    native_text_sample_interval: int = 10

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @classmethod
    def from_env(cls) -> ConversionConfig:
        config = cls(
            data_dir=Path(os.environ.get("INKSLIM_DATA_DIR", "/data/inkslim")).expanduser(),
            max_ocr_pages=_read_int("MAX_OCR_PAGES", 0, minimum=0),
            ocr_workers=_read_int("OCR_WORKERS", 1, minimum=1),
            ocr_timeout_page=_read_int("OCR_TIMEOUT_PAGE", 120, minimum=1),
            job_retention_hours=_read_int("JOB_RETENTION_HOURS", 24, minimum=1),
            max_conversion_storage_mb=_read_int("MAX_CONVERSION_STORAGE_MB", 5120, minimum=1),
            queue_poll_seconds=_read_float("QUEUE_POLL_SECONDS", 2.0, minimum=0.1),
            pdf_render_dpi=_read_int("PDF_RENDER_DPI", 300, minimum=72),
            min_render_dpi=_read_int("MIN_RENDER_DPI", 120, minimum=72),
            max_render_pixels=_read_int("MAX_RENDER_PIXELS", 50_000_000, minimum=1),
            max_render_dimension=_read_int("MAX_RENDER_DIMENSION", 12_000, minimum=1),
            native_text_sample_interval=_read_int("NATIVE_TEXT_SAMPLE_INTERVAL", 10, minimum=1),
        )
        if config.ocr_workers != 1:
            raise ValueError("OCR_WORKERS must remain 1 in conversion V1")
        if config.min_render_dpi > config.pdf_render_dpi:
            raise ValueError("MIN_RENDER_DPI cannot exceed PDF_RENDER_DPI")
        return config


def _read_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _read_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value
