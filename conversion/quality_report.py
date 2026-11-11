"""Build a privacy-conscious JSON quality report from durable checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .models import Job, PageResult, TextSource
from .language import detect_page_language, ocr_languages_for_selection
from .pdf.models import PageAnalysis, PageDecision
from .pdf.content_classification import classify_page_content
from .storage import JobWorkspace
from .text_fidelity import compare_text_fidelity, summarize_text_fidelity

REPORT_SCHEMA_VERSION = 9
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




def _document_structure_diagnostics(workspace: JobWorkspace, total_pages: int) -> dict[str, Any]:
    """Report likely EPUB structure defects without including recognized book text."""
    model = workspace.load_document_model()
    if model is None:
        return {
            "status": "unavailable",
            "reading_order": {"status": "not_evaluated", "out_of_order_transitions": 0, "page_transitions": []},
            "duplicate_headings": {"count": 0, "occurrences": []},
            "possible_page_number_artifacts": {"count": 0, "page_numbers": []},
            "warnings": [],
        }

    heading_occurrences: dict[str, list[int | None]] = {}
    page_number_candidates: set[int] = set()
    source_pages: list[int] = []

    for chapter in model.chapters:
        chapter_headings: set[str] = set()
        chapter_page = next(
            (paragraph.source_page for paragraph in chapter.paragraphs
             if paragraph.source_page is not None),
            None,
        )
        chapter_title = unicodedata.normalize("NFKC", chapter.title).casefold()
        chapter_title = re.sub(r"[^\w]+", " ", chapter_title, flags=re.UNICODE).strip()
        if len(chapter_title) >= 4:
            chapter_headings.add(chapter_title)

        for paragraph in chapter.paragraphs:
            page = paragraph.source_page
            if page is not None:
                source_pages.append(page)
            text = " ".join(paragraph.text.split()).strip()
            if not text:
                continue
            if paragraph.kind in {"h1", "h2", "h3"}:
                normalized = unicodedata.normalize("NFKC", text).casefold()
                normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()
                if len(normalized) >= 4:
                    chapter_headings.add(normalized)
            elif (
                paragraph.kind == "p"
                and page is not None
                and re.fullmatch(r"\d{1,4}", text)
                and 1 <= int(text) <= total_pages
                and int(text) in {page, page - 1}
            ):
                page_number_candidates.add(page)

        for heading in chapter_headings:
            heading_occurrences.setdefault(heading, []).append(chapter_page)

    transitions = [
        {"from_page": previous, "to_page": current}
        for previous, current in zip(source_pages, source_pages[1:])
        if current < previous
    ]
    duplicate_headings = [
        {
            "occurrence_count": len(pages),
            "page_numbers": sorted({page for page in pages if page is not None})[:100],
        }
        for pages in heading_occurrences.values()
        if len(pages) > 1
    ]
    duplicate_headings.sort(key=lambda item: (-item["occurrence_count"], item["page_numbers"]))
    duplicate_headings = duplicate_headings[:100]

    warnings: list[dict[str, Any]] = []
    if transitions:
        warnings.append({
            "code": "SOURCE_PAGE_ORDER_REGRESSION",
            "message": "Reconstructed paragraphs do not follow monotonically increasing source-page order.",
            "affected_transition_count": len(transitions),
            "transitions": transitions[:100],
        })
    if duplicate_headings:
        warnings.append({
            "code": "POSSIBLE_DUPLICATE_HEADINGS",
            "message": "Identical heading text occurs more than once; review whether repeated headings are intentional.",
            "affected_heading_count": len(duplicate_headings),
            "occurrences": duplicate_headings,
        })
    if page_number_candidates:
        warnings.append({
            "code": "POSSIBLE_PAGE_NUMBER_ARTIFACTS",
            "message": "Short numeric paragraphs match their source page number and may be printed page numbers.",
            "affected_page_count": len(page_number_candidates),
            "page_numbers": sorted(page_number_candidates)[:100],
        })

    return {
        "status": "available",
        "reading_order": {
            "status": "review" if transitions else "ok",
            "out_of_order_transitions": len(transitions),
            "page_transitions": transitions[:100],
        },
        "duplicate_headings": {
            "count": len(duplicate_headings),
            "occurrences": duplicate_headings,
        },
        "possible_page_number_artifacts": {
            "count": len(page_number_candidates),
            "page_numbers": sorted(page_number_candidates)[:100],
        },
        "warnings": warnings,
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


def _duplicate_cover_statistics(
    workspace: JobWorkspace,
    total_pages: int,
    classifications: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Diagnose exact and perceptual cover duplicates without modifying assets."""
    result: dict[str, Any] = {
        "status": "not_evaluated",
        "method": "sha256_exact_file_bytes_and_dhash64",
        "cover_candidate": None,
        "duplicate_count": 0,
        "matches": [],
        "near_duplicate_count": 0,
        "near_matches": [],
        "perceptual_hamming_threshold": 5,
        "removed_count": 0,
        "automatic_removal_enabled": False,
        "warnings": [],
    }
    first_checkpoint = workspace.load_page_images(1)
    if (
        first_checkpoint is None
        or first_checkpoint.detected_count is None
        or first_checkpoint.detected_count != len(first_checkpoint.images)
    ):
        result["status"] = "incomplete"
        result["warnings"].append("FIRST_PAGE_IMAGE_CHECKPOINT_INCOMPLETE")
        return result

    classification = classifications.get(1, {})
    try:
        confidence = float(classification.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if classification.get("type") != "cover_candidate" or confidence < 0.75:
        return result
    candidates = [image for image in first_checkpoint.images if image.coverage >= 0.45]
    if not candidates:
        return result
    cover = max(candidates, key=lambda image: image.coverage)
    result["cover_candidate"] = {
        "page_number": 1,
        "image_index": cover.image_index,
        "coverage": round(cover.coverage, 4),
        "classification_confidence": round(confidence, 2),
    }

    root = workspace.root.resolve()

    def safe_asset(image: Any) -> Path | None:
        relative = Path(image.source_path)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        asset = workspace.root / relative
        try:
            resolved = asset.resolve(strict=True)
            if (
                not resolved.is_relative_to(root)
                or asset.is_symlink()
                or not resolved.is_file()
            ):
                return None
            return resolved
        except (OSError, RuntimeError):
            return None

    def exact_digest(image: Any) -> tuple[str, int] | None:
        resolved = safe_asset(image)
        if resolved is None:
            return None
        try:
            digest = hashlib.sha256()
            size = 0
            with resolved.open("rb") as stream:
                header = stream.read(8)
                if image.media_type == "image/png" and header != b"\x89PNG\r\n\x1a\n":
                    return None
                if image.media_type == "image/jpeg" and not header.startswith(b"\xff\xd8\xff"):
                    return None
                stream.seek(0)
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            return (digest.hexdigest(), size) if size > 8 else None
        except OSError:
            return None

    def difference_hash(image: Any) -> int | None:
        resolved = safe_asset(image)
        if resolved is None:
            return None
        try:
            with Image.open(resolved) as opened:
                opened.verify()
            with Image.open(resolved) as opened:
                grayscale = opened.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
                pixels = list(grayscale.getdata())
            value = 0
            for row in range(8):
                offset = row * 9
                for column in range(8):
                    value = (value << 1) | int(
                        pixels[offset + column] > pixels[offset + column + 1]
                    )
            return value
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            return None

    cover_digest = exact_digest(cover)
    cover_hash = difference_hash(cover)
    if cover_digest is None or cover_hash is None:
        result["status"] = "incomplete"
        result["warnings"].append("COVER_IMAGE_UNREADABLE_OR_INVALID")
        return result

    cover_ratio = cover.width / cover.height if cover.height else 0
    complete = True
    exact_matches: list[dict[str, int]] = []
    near_matches: list[dict[str, int]] = []
    exact_keys: set[tuple[int, int]] = set()

    for page_number in range(2, total_pages + 1):
        checkpoint = workspace.load_page_images(page_number)
        if checkpoint is None or checkpoint.detected_count is None:
            complete = False
            result["warnings"].append(f"IMAGE_CHECKPOINT_MISSING:{page_number}")
            continue
        if checkpoint.detected_count != len(checkpoint.images):
            complete = False
        for image in checkpoint.images:
            key = (page_number, image.image_index)
            digest = exact_digest(image)
            perceptual_hash = difference_hash(image) if image.coverage >= 0.40 else None
            if digest is None:
                complete = False
                result["warnings"].append(f"IMAGE_ASSET_UNREADABLE:{page_number}:{image.image_index}")
            elif digest == cover_digest:
                exact_matches.append({"page_number": page_number, "image_index": image.image_index})
                exact_keys.add(key)

            if image.coverage < 0.40:
                continue
            ratio = image.width / image.height if image.height else 0
            if not ratio or not cover_ratio or abs(ratio - cover_ratio) > 0.08:
                continue
            if perceptual_hash is None:
                complete = False
                continue
            distance = (cover_hash ^ perceptual_hash).bit_count()
            if distance <= 5 and key not in exact_keys:
                near_matches.append({
                    "page_number": page_number,
                    "image_index": image.image_index,
                    "hamming_distance": distance,
                })

    result["status"] = "complete" if complete else "incomplete"
    result["matches"] = exact_matches
    result["duplicate_count"] = len(exact_matches)
    result["near_matches"] = near_matches
    result["near_duplicate_count"] = len(near_matches)
    return result

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
    content_classifications: dict[int, dict[str, Any]] = {}
    decisions: Counter[str] = Counter()
    content_types: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    qualities: Counter[str] = Counter()
    warning_count = 0
    text_fidelity_pages: list[dict[str, Any]] = []
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
        recognized_text_for_classification = (
            "\n".join(block.text for block in result.blocks) if result is not None else ""
        )
        page["content_classification"] = classify_page_content(
            analysis,
            page_images,
            recognized_text=recognized_text_for_classification,
            text_source=result.source.value if result is not None else None,
        )
        content_classifications[page_number] = page["content_classification"]
        content_types[page["content_classification"]["type"]] += 1
        recognized_text = (
            "\n".join(block.text for block in result.blocks) if result is not None else ""
        )
        fidelity = compare_text_fidelity(recognized_text, page_text)
        page["text_fidelity"] = fidelity
        if fidelity["status"] != "no_source_text":
            text_fidelity_pages.append(fidelity)
        if result is not None:
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
    document_structure = _document_structure_diagnostics(workspace, total_pages)
    image_stats = _image_statistics(workspace, total_pages)
    image_stats["duplicate_cover_detection"] = _duplicate_cover_statistics(
        workspace, total_pages, content_classifications
    )
    image_stats["optimized_image_count"] = 0
    image_stats["optimization_saved_bytes"] = 0
    image_stats["optimization_method"] = (
        "jpeg_quality_85_optimize_progressive_png_lossless_optimize"
    )
    document_language = workspace.load_document_language_analysis()
    text_fidelity_stats = summarize_text_fidelity(text_fidelity_pages)
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
                optimized_count = 0
                saved_bytes = 0
                for page_number in range(1, total_pages + 1):
                    checkpoint = workspace.load_page_images(page_number)
                    if checkpoint is None:
                        continue
                    for image in checkpoint.images:
                        archive_name = "OEBPS/" + image.source_path
                        if archive_name not in embedded_assets:
                            continue
                        source_asset = workspace.root / image.source_path
                        try:
                            resolved_asset = source_asset.resolve(strict=True)
                            if (
                                not resolved_asset.is_relative_to(workspace.root.resolve())
                                or source_asset.is_symlink()
                                or not resolved_asset.is_file()
                            ):
                                continue
                            source_size = resolved_asset.stat().st_size
                            embedded_size = archive.getinfo(archive_name).file_size
                        except (OSError, RuntimeError):
                            continue
                        if embedded_size < source_size:
                            optimized_count += 1
                            saved_bytes += source_size - embedded_size
                image_stats["optimized_image_count"] = optimized_count
                image_stats["optimization_saved_bytes"] = saved_bytes
                image_stats["status"] = (
                    "embedded" if embedded_assets else
                    "not_yet_embedded" if image_stats["extracted_count"] else
                    "no_images" if image_stats["detection_complete"] and image_stats["detected_count"] == 0
                    else "incomplete"
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
            "version": os.environ.get("INKSLIM_VERSION", "0.1.0-dev").removeprefix("v"),
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
        "document_structure": document_structure,
        "images": image_stats,
        "duplicate_covers": image_stats["duplicate_cover_detection"],
        "document_language": document_language or {
            "schema_version": 1,
            "method": "unavailable",
            "language": None,
            "evaluated_pages": 0,
            "supporting_pages": 0,
            "vote_share": None,
        },
        "text_fidelity": text_fidelity_stats,
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
            "page_content_types": dict(sorted(content_types.items())),
            "decisions": {
                decision.value: decisions.get(decision.value, 0)
                for decision in PageDecision
            },
            "pages_with_results": sum(sources.values()),
            "result_sources": dict(sorted(sources.items())),
            "result_qualities": dict(sorted(qualities.items())),
            "warning_count": warning_count + len(content_warnings) + len(document_structure["warnings"]),
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
