"""Orchestrate EPUB 3 generation from durable conversion checkpoints."""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from ..document import build_document_model
from ..language import epub_language_for_selection
from ..models import Job, JobStatus
from ..storage import JobStore, JobWorkspace
from ..worker import JobCancelled, JobProcessingError
from .builder import EpubBuildError, build_epub

logger = logging.getLogger(__name__)


def is_valid_epub(path: Path | str) -> bool:
    """Verify that path points to a well-formed, minimal EPUB 3 archive."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 100:
        return False
    try:
        with zipfile.ZipFile(path, "r") as archive:
            first = archive.infolist()[0]
            if first.filename != "mimetype":
                return False
            if first.compress_type != zipfile.ZIP_STORED:
                return False
            if archive.read("mimetype").strip() != b"application/epub+zip":
                return False
            if archive.testzip() is not None:
                return False
            names = set(archive.namelist())
            if "META-INF/container.xml" not in names:
                return False
            if "OEBPS/content.opf" not in names:
                return False
            if "OEBPS/nav.xhtml" not in names:
                return False
        return True
    except (zipfile.BadZipFile, OSError, IndexError, KeyError):
        return False


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
    """Build a complete EPUB 3 output from durable page checkpoints.

    Reuses an existing valid EPUB on disk if present.  Otherwise, loads
    cleaned paragraphs, structure checkpoints, builds the DocumentModel,
    persists the document model checkpoint and builds the EPUB atomically.
    """
    if total_pages < 1:
        raise ValueError("total_pages must be at least 1")

    output_filename = f"{_safe_stem(job.input_name)}.epub"
    destination = workspace.output_dir / output_filename

    # Resumability: reuse an existing valid EPUB output if already present
    if is_valid_epub(destination):
        logger.info("Reusing existing valid EPUB output for job %s: %s", job.id, destination)
        return destination

    if store and store.is_cancel_requested(job.id):
        raise JobCancelled("Cancellation requested before EPUB generation")

    cleaned_pages: dict[int, list[dict]] = {}
    for page_number in range(1, total_pages + 1):
        paragraphs = workspace.load_page_cleaned_paragraphs(page_number)
        if paragraphs is None:
            raise JobProcessingError(
                "CLEANED_TEXT_CHECKPOINT_MISSING",
                f"Cleaned text for page {page_number} is missing.",
            )
        cleaned_pages[page_number] = paragraphs

    structure_analysis = workspace.load_structure_analysis()
    if structure_analysis is None:
        raise JobProcessingError(
            "STRUCTURE_CHECKPOINT_MISSING",
            "Structure analysis data is missing; document cannot be assembled.",
        )

    page_annotations = {
        page_number: workspace.load_page_structure(page_number) or []
        for page_number in range(1, total_pages + 1)
    }

    title = _display_title(job.input_name)
    language = epub_language_for_selection(job.language)

    doc = build_document_model(
        cleaned_pages,
        structure_analysis,
        page_annotations,
        total_pages=total_pages,
        title=title,
        language=language,
    )
    workspace.save_document_model(doc)

    if store and store.is_cancel_requested(job.id):
        raise JobCancelled("Cancellation requested before EPUB assembly")

    if store:
        store.update_progress(
            job.id,
            status=JobStatus.BUILDING_EPUB,
            current_page=total_pages,
            total_pages=total_pages,
            message="Assembling EPUB document...",
        )

    try:
        build_epub(
            destination=destination,
            metadata=doc.metadata,
            chapters=doc.to_epub_chapters(),
        )
    except EpubBuildError as exc:
        raise JobProcessingError("EPUB_BUILD_FAILED", f"EPUB generation failed: {exc}") from exc
    except OSError as exc:
        raise JobProcessingError("STORAGE_ERROR", "Could not save the generated EPUB file.") from exc

    if not is_valid_epub(destination):
        destination.unlink(missing_ok=True)
        raise JobProcessingError(
            "EPUB_BUILD_FAILED",
            "The generated EPUB file failed container verification.",
        )

    logger.info("Generated EPUB for job %s at %s (%d bytes)", job.id, destination, destination.stat().st_size)
    return destination
