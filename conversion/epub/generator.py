"""Orchestrate EPUB 3 generation from durable conversion checkpoints."""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pikepdf

from ..document import build_document_model
from ..pdf.content_classification import classify_page_content
from ..language import epub_language_for_selection, infer_document_language
from ..models import Job, JobStatus
from ..storage import JobStore, JobWorkspace
from ..worker import JobCancelled, JobProcessingError
from .builder import EPUB_LAYOUT_VERSION, EpubBuildError, build_epub, clean_text
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


def _filename_metadata(name: str) -> tuple[str, str | None]:
    """Derive a title and optional author from the common 'Title - Author' filename form."""
    stem = Path(name).stem.strip()
    separator = " - "
    if separator in stem:
        title, author = stem.split(separator, 1)
        if title.strip() and author.strip():
            return title.strip(), author.strip()
    cleaned = re.sub(r"[_-]+", " ", stem).strip()
    return (cleaned.title() if cleaned else "Book"), None

def _read_pdf_metadata(path: str | Path) -> dict[str, str]:
    """Read basic PDF document information without making conversion depend on it."""
    try:
        with pikepdf.open(path) as pdf:
            return {
                key: str(pdf.docinfo.get(key)).strip()
                for key in ("/Title", "/Author")
                if pdf.docinfo.get(key) and str(pdf.docinfo.get(key)).strip()
            }
    except Exception:
        logger.debug("Could not read optional PDF metadata from %s", path, exc_info=True)
        return {}



def _epub_layout_matches(path: Path) -> bool:
    """Return whether a cached EPUB uses the current paragraph/typography rules."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            stylesheet = archive.read("OEBPS/styles.css").decode("utf-8")
        return f"Inkslim EPUB layout version: {EPUB_LAYOUT_VERSION}" in stylesheet
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
        return False


def _exclude_cover_ocr_text(
    cleaned_pages: dict[int, list[dict]],
    page_classifications: dict[int, dict],
) -> tuple[dict[int, list[dict]], list[int]]:
    """Omit OCR text from an image-dominated cover page, keeping its image intact."""
    cover = page_classifications.get(1, {})
    signals = cover.get("signals", {})
    if not (
        cover.get("type") == "cover_candidate"
        and signals.get("text_source") == "tesseract"
        and signals.get("text_word_count", 0) < 20
        and signals.get("largest_image_coverage", 0.0) >= 0.45
        and cover.get("confidence", 0.0) >= 0.75
        and len(cleaned_pages) > 1
    ):
        return cleaned_pages, []
    return {page: paragraphs for page, paragraphs in cleaned_pages.items() if page != 1}, [1]


def _epub_metadata_matches(path: Path, title: str, author: str | None) -> bool:
    """Return whether a cached EPUB contains the currently resolved book metadata."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            root = ET.fromstring(archive.read("OEBPS/content.opf"))
        metadata = root.find("{http://www.idpf.org/2007/opf}metadata")
        if metadata is None:
            return False
        dc_ns = "{http://purl.org/dc/elements/1.1/}"
        title_node = metadata.find(f"{dc_ns}title")
        creator_node = metadata.find(f"{dc_ns}creator")
        cached_title = clean_text(title_node.text or "") if title_node is not None else ""
        cached_author = (
            clean_text(creator_node.text or "") if creator_node is not None else None
        )
        expected_author = clean_text(author) if author else None
        return cached_title == title and cached_author == expected_author
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return False


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
    filename_title, filename_author = _filename_metadata(job.input_name)
    pdf_metadata = _read_pdf_metadata(job.input_path)
    resolved_title = clean_text(pdf_metadata.get("/Title", "")) or filename_title
    resolved_author = clean_text(pdf_metadata.get("/Author", "")) or filename_author

    expected_images = sum(
        len(checkpoint.images)
        for page in range(1, total_pages + 1)
        if (checkpoint := workspace.load_page_images(page)) is not None
    )
    if (
        is_valid_epub(destination)
        and embedded_image_count(destination) == expected_images
        and (expected_images == 0 or has_current_image_layout(destination))
        and _epub_layout_matches(destination)
        and workspace.load_document_language_analysis() is not None
        and _epub_metadata_matches(destination, resolved_title, resolved_author)
    ):
        logger.info("Reusing existing valid EPUB output for job %s: %s", job.id, destination)
        return destination
    if store and store.is_cancel_requested(job.id):
        raise JobCancelled("Cancellation requested before EPUB generation")

    stitched_checkpoint = workspace.load_stitched_paragraphs(total_pages)
    if stitched_checkpoint is None:
        raise JobProcessingError(
            "STITCHED_TEXT_CHECKPOINT_MISSING",
            "Document-level paragraph stitching data is missing; the job must be processed again.",
        )
    cleaned_pages, _stitching_analysis = stitched_checkpoint

    structure_analysis = workspace.load_structure_analysis()
    if structure_analysis is None:
        raise JobProcessingError("STRUCTURE_CHECKPOINT_MISSING",
                                 "Structure analysis data is missing; document cannot be assembled.")
    page_annotations = {
        number: workspace.load_page_structure(number) or []
        for number in range(1, total_pages + 1)
    }
    page_classifications = (
        _page_content_classifications(workspace, total_pages) if expected_images else {}
    )
    epub_pages, omitted_cover_pages = _exclude_cover_ocr_text(
        cleaned_pages, page_classifications
    )
    if omitted_cover_pages:
        logger.info(
            "Omitting low-volume OCR text from image-dominated cover page(s) %s for job %s",
            omitted_cover_pages, job.id,
        )

    language_pages = {
        page_number: " ".join(
            str(paragraph.get("text", "")).strip()
            for paragraph in paragraphs
            if str(paragraph.get("text", "")).strip()
        )
        for page_number, paragraphs in epub_pages.items()
    }
    document_language, language_diagnostics = infer_document_language(
        language_pages,
        fallback_selection=job.language,
    )
    workspace.save_document_language_analysis(language_diagnostics)

    doc = build_document_model(
        epub_pages, structure_analysis, page_annotations, total_pages=total_pages,
        title=resolved_title,
        language=document_language,
        author=resolved_author,
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
                page_classifications=page_classifications or None,
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
