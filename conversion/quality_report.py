"""Build a privacy-conscious JSON quality report from durable checkpoints."""

from __future__ import annotations

import json
import os
import re
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Job, PageResult, TextSource
from .language import detect_page_language, ocr_languages_for_selection
from .pdf.models import PageAnalysis, PageDecision
from .storage import JobWorkspace

REPORT_SCHEMA_VERSION = 3
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




def _image_statistics(workspace: JobWorkspace, total_pages: int) -> dict[str, Any]:
    """Summarize extracted image assets without exposing their internal paths."""
    pages_scanned = 0
    pages_with_images: set[int] = set()
    detected_count = 0
    extracted_count = 0
    not_extracted_count = 0
    missing_asset_count = 0
    extracted_bytes = 0
    scan_failure_pages: list[int] = []
    missing_checkpoint_pages: list[int] = []
    warning_pages: dict[str, set[int]] = {}

    root = workspace.root.resolve()
    for page_number in range(1, total_pages + 1):
        checkpoint = workspace.load_page_images(page_number)
        if checkpoint is None:
            missing_checkpoint_pages.append(page_number)
            continue

        pages_scanned += 1
        if checkpoint.detected_count is None:
            scan_failure_pages.append(page_number)
        else:
            detected_count += checkpoint.detected_count
            not_extracted_count += max(0, checkpoint.detected_count - len(checkpoint.images))
            if checkpoint.detected_count:
                pages_with_images.add(page_number)
        if checkpoint.images:
            pages_with_images.add(page_number)

        for warning in checkpoint.warnings:
            code = warning.split(":", 1)[0]
            warning_pages.setdefault(code, set()).add(page_number)

        for image in checkpoint.images:
            asset = workspace.root / image.source_path
            try:
                resolved = asset.resolve(strict=True)
                if not resolved.is_relative_to(root) or asset.is_symlink() or not resolved.is_file():
                    raise OSError("Image asset is not a regular file inside the job workspace")
                extracted_count += 1
                extracted_bytes += resolved.stat().st_size
            except (OSError, RuntimeError):
                missing_asset_count += 1

    warnings = [
        {"code": code, "affected_page_count": len(page_numbers),
         "page_numbers": sorted(page_numbers)[:100]}
        for code, page_numbers in sorted(warning_pages.items())
    ]
    detection_complete = not scan_failure_pages and not missing_checkpoint_pages
    return {
        "status": "not_yet_embedded",
        "pages_scanned": pages_scanned,
        "pages_with_images": len(pages_with_images),
        "detected_count": detected_count,
        "detection_complete": detection_complete,
        "extracted_count": extracted_count,
        "not_extracted_count": not_extracted_count,
        "missing_asset_count": missing_asset_count,
        "extracted_bytes": extracted_bytes,
        "embedded_in_epub": None,
        "embedded_bytes": None,
        "scan_failure_pages": scan_failure_pages[:100],
        "missing_checkpoint_pages": missing_checkpoint_pages[:100],
        "warnings": warnings,
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

        page_images = workspace.load_page_images(page_number)
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
            "images": (
                {
                    "detected_count": page_images.detected_count,
                    "extracted_count": len(page_images.images),
                    "warnings": list(page_images.warnings),
                } if page_images is not None else None
            ),
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
    image_stats = _image_statistics(workspace, total_pages)
    source_path = Path(job.input_path)
    source_size = source_path.stat().st_size if source_path.is_file() else None
    output = Path(output_path) if output_path is not None else None
    output_size = output.stat().st_size if output is not None and output.is_file() else None
    if output is not None and output.is_file():
        try:
            with zipfile.ZipFile(output, "r") as archive:
                embedded_assets = [
                    name for name in archive.namelist()
                    if name.startswith("OEBPS/images/")
                    and name.lower().endswith((".jpg", ".jpeg", ".png"))
                ]
                image_stats["embedded_in_epub"] = len(embedded_assets)
                image_stats["embedded_bytes"] = sum(
                    archive.getinfo(name).file_size for name in embedded_assets
                )
                image_stats["status"] = (
                    "embedded" if embedded_assets else
                    "not_yet_embedded" if image_stats["extracted_count"] else "no_images"
                )
        except (OSError, zipfile.BadZipFile):
            image_stats["status"] = "epub_unreadable"

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
        "generator": {
            "name": "inkslim",
            "version": os.environ.get("INKSLIM_VERSION", "development"),
            "commit": os.environ.get("INKSLIM_COMMIT") or None,
        },
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
        "images": image_stats,
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
