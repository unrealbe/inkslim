"""Build a privacy-conscious JSON quality report from durable page checkpoints."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Job, PageResult
from .language import detect_page_language, ocr_languages_for_selection
from .pdf.models import PageAnalysis, PageDecision
from .storage import JobWorkspace

REPORT_SCHEMA_VERSION = 1


def build_quality_report(
    job: Job,
    workspace: JobWorkspace,
    *,
    total_pages: int,
    generated_at: datetime | None = None,
) -> Path:
    """Aggregate analysis/OCR checkpoints without copying recognized book text."""
    if total_pages < 1:
        raise ValueError("total_pages must be at least 1")

    pages: list[dict[str, Any]] = []
    decisions: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    qualities: Counter[str] = Counter()
    warning_count = 0
    confidence_values: list[float] = []

    for page_number in range(1, total_pages + 1):
        analysis: PageAnalysis | None = workspace.load_page_analysis(page_number)
        if analysis is None:
            raise ValueError(f"Missing or invalid analysis checkpoint for page {page_number}")
        result: PageResult | None = workspace.load_page_result(page_number)

        decisions[analysis.decision.value] += 1
        if result is not None:
            sources[result.source.value] += 1
            qualities[result.quality.value] += 1
            if result.metrics.mean_ocr_confidence is not None:
                confidence_values.append(result.metrics.mean_ocr_confidence)

        warnings = list(analysis.warnings)
        if result is not None:
            warnings.extend(result.warnings)
        warning_count += len(warnings)

        similarity = None
        for warning in analysis.warnings:
            if warning.startswith("NATIVE_OCR_SIMILARITY:"):
                try:
                    similarity = float(warning.split(":", 1)[1])
                except ValueError:
                    similarity = None
                break

        page: dict[str, Any] = {
            "page_number": page_number,
            "decision": analysis.decision.value,
            "native_text": {
                "present": analysis.has_native_text,
                "character_count": analysis.character_count,
                "word_count": analysis.word_count,
                "quality_score": analysis.native_text_quality,
                "meaningful_character_ratio": analysis.meaningful_character_ratio,
            },
            "render": {
                "rotation": analysis.rotation,
                "dpi": analysis.render_dpi,
                "width_px": analysis.render_width_px,
                "height_px": analysis.render_height_px,
            },
            "native_ocr_similarity": similarity,
            "warnings": warnings,
            "result": None,
        }
        if result is not None:
            recognized_text = "\n".join(block.text for block in result.blocks)
            detected_language = detect_page_language(recognized_text)
            page["language"] = detected_language.to_dict() if detected_language else None
            page["result"] = {
                "source": result.source.value,
                "quality": result.quality.value,
                "block_count": len(result.blocks),
                "character_count": sum(len(block.text) for block in result.blocks),
                "mean_ocr_confidence": result.metrics.mean_ocr_confidence,
                "warnings": list(result.warnings),
            }
        pages.append(page)

    timestamp = generated_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")

    marginalia = workspace.load_marginalia_analysis()
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "pdf_page_analysis_and_ocr_quality",
        "generated_at": timestamp.astimezone(UTC).isoformat(),
        "job": {
            "id": job.id,
            "input_name": job.input_name,
            "language_selection": job.language,
            "ocr_languages": ocr_languages_for_selection(job.language),
            "total_pages": total_pages,
        },
        "marginalia": {
            "detected_items": len(marginalia["items"]) if marginalia else 0,
            "removed_paragraphs": marginalia["removed_paragraphs"] if marginalia else 0,
            "pages_with_removals": marginalia["pages_with_removals"] if marginalia else [],
        },
        "summary": {
            "total_pages": total_pages,
            "decisions": {
                decision.value: decisions.get(decision.value, 0)
                for decision in PageDecision
            },
            "pages_with_results": sum(sources.values()),
            "result_sources": dict(sorted(sources.items())),
            "result_qualities": dict(sorted(qualities.items())),
            "warning_count": warning_count,
            "mean_ocr_confidence": (
                round(sum(confidence_values) / len(confidence_values), 2)
                if confidence_values else None
            ),
        },
        "pages": pages,
        "privacy": {
            "recognized_text_included": False,
            "note": "The report contains aggregate metrics and page diagnostics, not book text.",
        },
    }

    destination = workspace.output_dir / "quality-report.json"
    workspace._atomic_json(destination, report)
    return destination
