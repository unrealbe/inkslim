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
    queue_poll_seconds: float = 2.0

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
            queue_poll_seconds=_read_float("QUEUE_POLL_SECONDS", 2.0, minimum=0.1),
        )
        if config.ocr_workers != 1:
            raise ValueError("OCR_WORKERS must remain 1 in conversion V1")
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
