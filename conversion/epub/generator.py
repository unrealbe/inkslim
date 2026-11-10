"""Orchestrate EPUB 3 generation from durable conversion checkpoints."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..document import build_document_model
from ..pdf.content_classification import classify_page_content
from ..language import epub_language_for_selection, infer_document_language
from ..models import Job, JobStatus
from ..storage import JobStore, JobWorkspace
from ..worker import JobCancelled, JobProcessingError
from .builder import EpubBuildError, build_epub
from .image_embedding import (
    EpubImageEmbeddingError,
    embed_pdf_images,
    embedded_image_count,
    has_current_image_layout,
)
from .validation import validate_epub

logger = logging.getLogger(__name__)


def is_valid_epub(path: Path | str) -> bool:
    """Return whether path contains a structurally usable EPUB 3 archive."""
    return not validate_epub(path)


def _safe_stem(name: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^\w\-.]", "_", stem).strip("._")
    return cleaned or "book"


def _display_title(name: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[_\-]+", " ", stem).strip()
    return cleaned.title() if cleaned else "Book"


def _page_content_classifications(workspace: JobWorkspace, total_pages: int) -> dict[int, dict]:
    """Build page classifications from durable analysis and text/image checkpoints."""
    classifications: dict[int, dict] = {}
    for page_number in range(1, total_pages + 1):
        analysis = workspace.load_page_analysis(page_number)
        if analysis is None:
            continue
        result = workspace.load_page_result(page_number)
        images = workspace.load_page_images(page_number)
        recognized_text = (
            "\n".join(block.text for block in result.blocks)
            if result is not None else ""
        )
        classifications[page_number] = classify_page_content(
            analysis,
            images,
            recognized_text=recognized_text,
            text_source=result.source.value if result is not None else None,
        )
    return classifications


def generate_job_epub(
    job: Job,
    workspace: JobWorkspace,
    *,
    total_pages: int,
    store: JobStore | None = None,
) -> Path:
    """Build a complete EPUB 3 output from durable checkpoints, including extracted images."""
    if total_pages < 1:
        raise ValueError("total_pages must be at least 1")
    destination = workspace.output_dir / f"{_safe_stem(job.input_name)}-{job.id[:8]}.epub"

    expected_images = sum(
        len(checkpoint.images)
        for page in range(1, total_pages + 1)
        if (checkpoint := workspace.load_page_images(page)) is not None
    )
    if (
        is_valid_epub(destination)
        and embedded_image_count(destination) == expected_images
        and (expected_images == 0 or has_current_image_layout(destination))
    ):
        logger.info("Reusing existing valid EPUB output for job %s: %s", job.id, destination)
        return destination
    if store and store.is_cancel_requested(job.id):
        raise JobCancelled("Cancellation requested before EPUB generation")

    cleaned_pages: dict[int, list[dict]] = {}
    for page_number in range(1, total_pages + 1):
        paragraphs = workspace.load_page_cleaned_paragraphs(page_number)
        if paragraphs is None:
            raise JobProcessingError("CLEANED_TEXT_CHECKPOINT_MISSING",
                                     f"Cleaned text for page {page_number} is missing.")
        cleaned_pages[page_number] = paragraphs

    structure_analysis = workspace.load_structure_analysis()
    if structure_analysis is None:
        raise JobProcessingError("STRUCTURE_CHECKPOINT_MISSING",
                                 "Structure analysis data is missing; document cannot be assembled.")
    page_annotations = {
        number: workspace.load_page_structure(number) or []
        for number in range(1, total_pages + 1)
    }
    language_pages = {
        page_number: " ".join(
            str(paragraph.get("text", "")).strip()
            for paragraph in paragraphs
            if str(paragraph.get("text", "")).strip()
        )
        for page_number, paragraphs in cleaned_pages.items()
    }
    document_language, language_diagnostics = infer_document_language(
        language_pages,
        fallback_selection=job.language,
    )
    workspace.save_document_language_analysis(language_diagnostics)

    doc = build_document_model(
        cleaned_pages, structure_analysis, page_annotations, total_pages=total_pages,
        title=_display_title(job.input_name), language=document_language,
    )
    doc.metadata.identifier = f"urn:uuid:{job.id}"
    workspace.save_document_model(doc)

    if store and store.is_cancel_requested(job.id):
        raise JobCancelled("Cancellation requested before EPUB assembly")
    if store:
        store.update_progress(job.id, status=JobStatus.BUILDING_EPUB,
                              current_page=total_pages, total_pages=total_pages,
                              message="Assembling EPUB document...")
    try:
        build_epub(destination=destination, metadata=doc.metadata, chapters=doc.to_epub_chapters())
        try:
            embed_pdf_images(
                destination,
                workspace,
                total_pages=total_pages,
                page_classifications=(
                    _page_content_classifications(workspace, total_pages)
                    if expected_images else None
                ),
            )
        except OSError:
            destination.unlink(missing_ok=True)
            raise
    except EpubBuildError as exc:
        raise JobProcessingError("EPUB_BUILD_FAILED", f"EPUB generation failed: {exc}") from exc
    except EpubImageEmbeddingError as exc:
        destination.unlink(missing_ok=True)
        raise JobProcessingError("EPUB_IMAGE_EMBEDDING_FAILED",
                                 "One or more extracted PDF images could not be included in the EPUB.") from exc
    except OSError as exc:
        raise JobProcessingError("STORAGE_ERROR", "Could not save the generated EPUB file.") from exc

    errors = validate_epub(destination)
    if errors:
        destination.unlink(missing_ok=True)
        logger.error("Generated EPUB failed structural validation for job %s: %s", job.id, "; ".join(errors))
        raise JobProcessingError("EPUB_VALIDATION_FAILED",
                                 "The generated EPUB failed structural validation.")
    logger.info("Generated EPUB for job %s at %s (%d bytes)", job.id, destination, destination.stat().st_size)
    return destination
