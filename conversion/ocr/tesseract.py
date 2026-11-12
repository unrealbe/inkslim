"""Tesseract TSV OCR adapter.

This module deliberately returns page-level text blocks only. Reading-order repair,
paragraph reconstruction and post-processing belong to later pipeline stages.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import subprocess
import time
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from ..models import PageMetrics, PageResult, QualityStatus, TextBlock, TextSource

logger = logging.getLogger(__name__)

_TSV_FIELDS = (
    "level", "page_num", "block_num", "par_num", "line_num", "word_num",
    "left", "top", "width", "height", "conf", "text",
)
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_MAX_IMAGE_PIXELS = 50_000_000
_HIGH_CONFIDENCE_THRESHOLD = 70.0


class OcrError(RuntimeError):
    """Base class for safe, user-displayable OCR failures."""

    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


class OcrEngine(Protocol):
    def recognize(
        self,
        image_path: Path,
        languages: list[str],
        *,
        page_number: int = 1,
    ) -> PageResult:
        """Recognize text in one already-rendered page image."""


class TesseractOcrEngine:
    """Run Tesseract without a shell and convert its TSV word data into text lines."""

    def __init__(
        self,
        *,
        executable: str = "tesseract",
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        max_image_pixels: int = _DEFAULT_MAX_IMAGE_PIXELS,
        high_confidence_threshold: float = _HIGH_CONFIDENCE_THRESHOLD,
    ) -> None:
        if not executable or "\x00" in executable:
            raise ValueError("executable must be a non-empty path")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if max_image_pixels < 1:
            raise ValueError("max_image_pixels must be positive")
        if not math.isfinite(high_confidence_threshold) or not 0 <= high_confidence_threshold <= 100:
            raise ValueError("high_confidence_threshold must be between 0 and 100")
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.max_image_pixels = max_image_pixels
        self.high_confidence_threshold = high_confidence_threshold

    def recognize(
        self,
        image_path: Path,
        languages: list[str],
        *,
        page_number: int = 1,
    ) -> PageResult:
        path = Path(image_path)
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        language_argument = self._language_argument(languages)
        width, height = self._read_image_dimensions(path)

        page_deadline = time.monotonic() + self.timeout_seconds
        first = self._recognize_with_psm(
            path, language_argument, page_number=page_number,
            width=width, height=height, psm=3,
        )
        first_confidence = first.metrics.mean_ocr_confidence
        if (
            not first.blocks
            or first_confidence is None
            or first_confidence >= self.high_confidence_threshold
        ):
            return first

        # A second layout analysis is deliberately limited to low-confidence
        # pages. The first result remains the fallback if retry fails or is
        # materially less complete.
        remaining_timeout = page_deadline - time.monotonic()
        if remaining_timeout <= 0:
            first.warnings.append("OCR_RETRY_PSM_6_SKIPPED_TIMEOUT_BUDGET")
            return first

        try:
            retry = self._recognize_with_psm(
                path, language_argument, page_number=page_number,
                width=width, height=height, psm=6,
                timeout_seconds=remaining_timeout,
            )
        except OcrError:
            first.warnings.append("OCR_RETRY_PSM_6_FAILED")
            return first

        if self._should_use_retry(first, retry):
            retry.warnings.append("OCR_RETRY_PSM_6_SELECTED")
            return retry

        first.warnings.append("OCR_RETRY_PSM_6_NOT_SELECTED")
        return first

    def _recognize_with_psm(
        self,
        path: Path,
        language_argument: str,
        *,
        page_number: int,
        width: int,
        height: int,
        psm: int,
        timeout_seconds: float | None = None,
    ) -> PageResult:
        command = [
            self.executable,
            str(path),
            "stdout",
            "-l",
            language_argument,
            "--psm",
            str(psm),
            "tsv",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrError("OCR_TIMEOUT", "Text recognition exceeded the page time limit.") from exc
        except OSError as exc:
            logger.warning("Could not start Tesseract: %s", exc)
            raise OcrError("OCR_ENGINE_UNAVAILABLE", "The text recognition engine could not be started.") from exc

        if completed.returncode != 0:
            logger.warning(
                "Tesseract exited with status %s: %s",
                completed.returncode,
                (completed.stderr or "")[-1000:],
            )
            raise OcrError("OCR_FAILED", "Text could not be recognized on this page.")

        blocks, confidences = self._parse_tsv(completed.stdout, width, height)
        mean_confidence = (
            sum(confidences) / len(confidences) if confidences else None
        )
        has_text = bool(blocks)
        quality = (
            QualityStatus.HIGH
            if has_text and mean_confidence is not None
            and mean_confidence >= self.high_confidence_threshold
            else QualityStatus.REVIEW
        )
        warnings = []
        if not has_text:
            warnings.append("OCR_NO_TEXT")
        elif mean_confidence is None:
            warnings.append("OCR_CONFIDENCE_UNAVAILABLE")
        elif mean_confidence < self.high_confidence_threshold:
            warnings.append("LOW_OCR_CONFIDENCE")

        return PageResult(
            page_number=page_number,
            width=float(width),
            height=float(height),
            blocks=blocks,
            source=TextSource.TESSERACT,
            quality=quality,
            metrics=PageMetrics(mean_ocr_confidence=mean_confidence),
            warnings=warnings,
        )

    @staticmethod
    def _should_use_retry(first: PageResult, retry: PageResult) -> bool:
        """Accept PSM 6 only when confidence improves without losing much text."""
        first_confidence = first.metrics.mean_ocr_confidence
        retry_confidence = retry.metrics.mean_ocr_confidence
        if (
            not retry.blocks
            or first_confidence is None
            or retry_confidence is None
            or retry_confidence < first_confidence + 3.0
        ):
            return False

        first_word_count = sum(len(block.text.split()) for block in first.blocks)
        retry_word_count = sum(len(block.text.split()) for block in retry.blocks)
        minimum_word_count = max(1, math.ceil(first_word_count * 0.8))
        return retry_word_count >= minimum_word_count

    def _read_image_dimensions(self, path: Path) -> tuple[int, int]:
        try:
            with Image.open(path) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > self.max_image_pixels:
                    raise OcrError(
                        "OCR_IMAGE_LIMIT",
                        "The rendered page image exceeds the recognition size limit.",
                    )
                image.verify()
            return width, height
        except OcrError:
            raise
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise OcrError("OCR_INVALID_IMAGE", "The rendered page image is invalid or unreadable.") from exc

    @staticmethod
    def _language_argument(languages: list[str]) -> str:
        if not languages:
            raise ValueError("at least one OCR language is required")
        normalized: list[str] = []
        for language in languages:
            if not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
                raise ValueError(f"invalid OCR language code: {language!r}")
            if language not in normalized:
                normalized.append(language)
        return "+".join(normalized)

    @staticmethod
    def _parse_tsv(
        content: str,
        image_width: int,
        image_height: int,
    ) -> tuple[list[TextBlock], list[float]]:
        reader = csv.DictReader(io.StringIO(content), delimiter="\t")
        if reader.fieldnames is None or any(field not in reader.fieldnames for field in _TSV_FIELDS):
            raise OcrError("OCR_INVALID_OUTPUT", "The text recognition engine returned invalid data.")

        # OrderedDict preserves Tesseract's page reading sequence while combining
        # the individual word rows into one TextBlock per OCR line.
        lines: OrderedDict[tuple[int, int, int], dict[str, object]] = OrderedDict()
        all_confidences: list[float] = []
        try:
            for row in reader:
                if row.get("level") != "5":
                    continue
                text = (row.get("text") or "").strip()
                if not text:
                    continue
                block_num = int(row["block_num"])
                paragraph_num = int(row["par_num"])
                line_num = int(row["line_num"])
                left = int(row["left"])
                top = int(row["top"])
                box_width = int(row["width"])
                box_height = int(row["height"])
                if min(left, top, box_width, box_height) < 0 or box_width < 1 or box_height < 1:
                    continue

                x0 = min(image_width, max(0, left))
                y0 = min(image_height, max(0, top))
                x1 = min(image_width, max(0, left + box_width))
                y1 = min(image_height, max(0, top + box_height))
                if x1 <= x0 or y1 <= y0:
                    continue

                confidence = TesseractOcrEngine._parse_confidence(row.get("conf"))
                if confidence is not None:
                    all_confidences.append(confidence)

                key = (block_num, paragraph_num, line_num)
                if key not in lines:
                    lines[key] = {
                        "words": [],
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "confidences": [],
                    }
                line = lines[key]
                line["words"].append(text)  # type: ignore[union-attr]
                line["x0"] = min(line["x0"], x0)  # type: ignore[arg-type]
                line["y0"] = min(line["y0"], y0)  # type: ignore[arg-type]
                line["x1"] = max(line["x1"], x1)  # type: ignore[arg-type]
                line["y1"] = max(line["y1"], y1)  # type: ignore[arg-type]
                if confidence is not None:
                    line["confidences"].append(confidence)  # type: ignore[union-attr]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise OcrError("OCR_INVALID_OUTPUT", "The text recognition engine returned invalid data.") from exc

        blocks: list[TextBlock] = []
        for (block_num, paragraph_num, line_num), line in lines.items():
            line_confidences = line["confidences"]
            confidence = (
                sum(line_confidences) / len(line_confidences)
                if line_confidences else None
            )
            blocks.append(
                TextBlock(
                    text=" ".join(line["words"]),
                    bbox=(
                        line["x0"] / image_width,
                        line["y0"] / image_height,
                        line["x1"] / image_width,
                        line["y1"] / image_height,
                    ),
                    block_type="text",
                    confidence=confidence,
                    source=TextSource.TESSERACT,
                    line_number=line_num,
                    paragraph_number=paragraph_num,
                )
            )
        return blocks, all_confidences

    @staticmethod
    def _parse_confidence(value: str | None) -> float | None:
        try:
            confidence = float(value) if value is not None else -1.0
        except (TypeError, ValueError):
            return None
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 100.0:
            return None
        return confidence
