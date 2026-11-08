"""Orchestrate EPUB 3 generation from durable conversion checkpoints."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..document import build_document_model
from ..language import epub_language_for_selection
from ..models import Job, JobStatus
from ..storage import JobStore, JobWorkspace
from ..worker import JobCancelled, JobProcessingError
from .builder import EpubBuildError, build_epub
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


def generate_job_epub(
    job: Job,
    workspace: JobWorkspace,
    *,
    total_pages: int,
    store: JobStore | None = None,
) -> Path:
    """Build a complete EPUB 3 output from durable conversion checkpoints."""
    if total_pages < 1:
        raise ValueError("total_pages must be at least 1")
    destination = workspace.output_dir / f"{_safe_stem(job.input_name)}-{job.id[:8]}.epub"

    if is_valid_epub(destination):
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
    doc = build_document_model(
        cleaned_pages, structure_analysis, page_annotations, total_pages=total_pages,
        title=_display_title(job.input_name), language=epub_language_for_selection(job.language),
    )
    # A new conversion is a distinct publication, even when the source filename
    # and book content are identical to an earlier job.
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
    except EpubBuildError as exc:
        raise JobProcessingError("EPUB_BUILD_FAILED", f"EPUB generation failed: {exc}") from exc
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
