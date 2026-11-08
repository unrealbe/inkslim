"""Build a privacy-conscious JSON quality report from durable checkpoints."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Job, PageResult, TextSource
from .language import detect_page_language, ocr_languages_for_selection
from .pdf.models import PageAnalysis, PageDecision
from .storage import JobWorkspace

REPORT_SCHEMA_VERSION = 2
_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
_EMPTY_DOCUMENT_PLACEHOLDER = "[Empty document]"


def _document_statistics(workspace: JobWorkspace) -> dict[str, int]:
    """Count content from the persisted document model, excluding its empty fallback."""
    model = workspace.load_document_model()
    if model is None:
        return {"chapter_count": 0, "paragraph_count": 0, "heading_count": 0, "word_count": 0}

    paragraphs = [
        paragraph
        for chapter in model.chapters
        for paragraph in chapter.paragraphs
        if paragraph.text.strip() and paragraph.text.strip() != _EMPTY_DOCUMENT_PLACEHOLDER
    ]
    return {
        "chapter_count": len(model.chapters),
        "paragraph_count": len(paragraphs),
        "heading_count": sum(paragraph.kind in {"h2", "h3"} for paragraph in paragraphs),
        "word_count": sum(len(_WORD_RE.findall(paragraph.text)) for paragraph in paragraphs),
    }


def build_quality_report(
    job: Job,
    workspace: JobWorkspace,
    *,
    total_pages: int,
    generated_at: datetime | None = None,
    output_path: Path | str | None = None,
) -> Path:
    """Aggregate diagnostics and EPUB statistics without copying recognized book text."""
    if total_pages < 1:
        raise ValueError("total_pages must be at least 1")

    pages: list[dict[str, Any]] = []
    decisions: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    qualities: Counter[str] = Counter()
    warning_count = 0
    confidence_values: list[float] = []
    pages_with_text = 0
    pages_without_text: list[int] = []
    ocr_pages = 0
    native_text_pages = 0
    empty_pages = 0
    ocr_pages_without_text: list[int] = []

    for page_number in range(1, total_pages + 1):
        analysis: PageAnalysis | None = workspace.load_page_analysis(page_number)
        if analysis is None:
            raise ValueError(f"Missing or invalid analysis checkpoint for page {page_number}")
        result: PageResult | None = workspace.load_page_result(page_number)
        cleaned = workspace.load_page_cleaned_paragraphs(page_number)
        page_text = " ".join(
            str(item.get("text", "")).strip()
            for item in (cleaned or [])
            if str(item.get("text", "")).strip()
        )
        has_text = bool(page_text)
        if has_text:
            pages_with_text += 1
        elif analysis.decision is not PageDecision.EMPTY:
            pages_without_text.append(page_number)

        if analysis.decision is PageDecision.EMPTY:
            empty_pages += 1
        elif analysis.decision is PageDecision.USE_NATIVE:
            native_text_pages += 1
        elif analysis.decision is PageDecision.OCR_REQUIRED:
            ocr_pages += 1
            if not has_text:
                ocr_pages_without_text.append(page_number)

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
            "has_extracted_text": has_text,
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
    structure = workspace.load_structure_analysis()
    content_stats = _document_statistics(workspace)
    source_path = Path(job.input_path)
    source_size = source_path.stat().st_size if source_path.is_file() else None
    output = Path(output_path) if output_path is not None else None
    output_size = output.stat().st_size if output is not None and output.is_file() else None

    content_warnings: list[dict[str, Any]] = []
    if content_stats["word_count"] == 0:
        content_warnings.append({
            "code": "NO_BODY_TEXT_EXTRACTED",
            "message": "No readable body text was reconstructed for the EPUB.",
        })
    if ocr_pages_without_text:
        content_warnings.append({
            "code": "OCR_PAGES_WITHOUT_TEXT",
            "message": "Some pages requiring OCR produced no reconstructed body text.",
            "page_numbers": ocr_pages_without_text[:100],
            "affected_page_count": len(ocr_pages_without_text),
        })
    if pages_without_text:
        content_warnings.append({
            "code": "PAGES_WITHOUT_TEXT",
            "message": "Some non-empty source pages have no reconstructed body text.",
            "page_numbers": pages_without_text[:100],
            "affected_page_count": len(pages_without_text),
        })

    eligible_pages = total_pages - empty_pages
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
        "structure": {
            "heading_count": structure["heading_count"] if structure else 0,
            "chapter_count": structure["chapter_count"] if structure else 0,
        },
        "output": {
            "format": "epub",
            "source_pdf_bytes": source_size,
            "epub_bytes": output_size,
            **content_stats,
        },
        "content_quality": {
            "pages_with_text": pages_with_text,
            "pages_without_text": len(pages_without_text),
            "empty_source_pages": empty_pages,
            "eligible_source_pages": eligible_pages,
            "text_page_coverage": (
                round(pages_with_text / eligible_pages, 4) if eligible_pages else None
            ),
            "warnings": content_warnings,
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
            "warning_count": warning_count + len(content_warnings),
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
