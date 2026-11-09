"""Engine-independent document model for EPUB generation.

This module bridges the gap between the per-page conversion pipeline output
(cleaned paragraphs, structure annotations) and the EPUB 3 writer.  Neither
side imports the other: the document model is the single shared contract.

Design principles:
- The model is built from durable JSON checkpoints, not from live OCR state.
- A document without any detected chapter headings is valid; the builder
  creates a single chapter titled after the document itself.
- Source paragraph text is never rewritten here; only ordering and grouping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .epub.builder import Block, Chapter, EpubMetadata


DOCUMENT_MODEL_VERSION = 2


@dataclass(frozen=True, slots=True)
class DocumentParagraph:
    """One paragraph destined for the EPUB, with its structural role."""

    text: str
    kind: str = "p"  # "p", "h2" or "h3"
    source_page: int | None = None
    paragraph_index: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    def to_block(self) -> Block:
        return Block(
            kind=self.kind,
            text=self.text,
            source_page=self.source_page,
            paragraph_index=self.paragraph_index,
            bbox=self.bbox,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "source_page": self.source_page,
            "paragraph_index": self.paragraph_index,
            "bbox": list(self.bbox) if self.bbox is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentParagraph:
        bbox_value = data.get("bbox")
        bbox = tuple(float(value) for value in bbox_value) if (
            isinstance(bbox_value, (list, tuple)) and len(bbox_value) == 4
        ) else None
        source_page = data.get("source_page")
        paragraph_index = data.get("paragraph_index")
        return cls(
            text=str(data.get("text", "")),
            kind=str(data.get("kind", "p")),
            source_page=source_page if type(source_page) is int and source_page > 0 else None,
            paragraph_index=paragraph_index if type(paragraph_index) is int and paragraph_index >= 0 else None,
            bbox=bbox,
        )


@dataclass(slots=True)
class DocumentChapter:
    """A chapter (or the single fallback chapter) in the output EPUB."""

    title: str
    paragraphs: list[DocumentParagraph] = field(default_factory=list)

    def to_epub_chapter(self) -> Chapter:
        return Chapter(
            title=self.title,
            blocks=[paragraph.to_block() for paragraph in self.paragraphs],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "paragraphs": [p.to_dict() for p in self.paragraphs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentChapter:
        return cls(
            title=str(data.get("title", "")),
            paragraphs=[
                DocumentParagraph.from_dict(p)
                for p in data.get("paragraphs", [])
            ],
        )


@dataclass(slots=True)
class DocumentModel:
    """Complete intermediate representation of a converted book.

    This is the single object that the EPUB generation step consumes.
    It is built once from checkpoint data and is independent of the OCR
    engine, the PDF renderer and the EPUB packaging format.
    """

    metadata: EpubMetadata
    chapters: list[DocumentChapter] = field(default_factory=list)

    @property
    def total_paragraphs(self) -> int:
        return sum(len(chapter.paragraphs) for chapter in self.chapters)

    def to_epub_chapters(self) -> list[Chapter]:
        return [chapter.to_epub_chapter() for chapter in self.chapters]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DOCUMENT_MODEL_VERSION,
            "metadata": {
                "title": self.metadata.title,
                "language": self.metadata.language,
                "author": self.metadata.author,
                "identifier": self.metadata.identifier,
                "toc_title": self.metadata.toc_title,
            },
            "chapters": [ch.to_dict() for ch in self.chapters],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentModel:
        if data.get("schema_version") not in {1, DOCUMENT_MODEL_VERSION}:
            raise ValueError("Unsupported DocumentModel schema version")
        meta = data.get("metadata", {})
        metadata = EpubMetadata(
            title=str(meta.get("title", "")),
            language=str(meta.get("language", "en")),
            author=meta.get("author"),
            identifier=meta.get("identifier"),
            toc_title=str(meta.get("toc_title", "Contents")),
        )
        chapters = [
            DocumentChapter.from_dict(ch)
            for ch in data.get("chapters", [])
        ]
        return cls(metadata=metadata, chapters=chapters)


def _paragraph_bbox(paragraph: dict[str, Any]) -> tuple[float, float, float, float] | None:
    value = paragraph.get("bbox")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        bbox = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
        return None
    return bbox


def _paragraph_text(paragraph: dict[str, Any]) -> str:
    """Extract and normalize paragraph text from a checkpoint dict."""
    text = paragraph.get("text", "")
    if not isinstance(text, str):
        return ""
    return " ".join(text.split()).strip()


def _heading_kind(annotation: dict[str, Any]) -> str:
    """Map a structure annotation kind to an EPUB block kind."""
    kind = annotation.get("kind", "")
    if kind == "chapter_heading":
        # Chapter headings become the chapter title, not an inline heading.
        # This function is only called for non-chapter-start annotations.
        return "h2"
    if kind == "section_heading":
        return "h2"
    if kind == "display_heading":
        return "h3"
    return "h3"


def build_document_model(
    cleaned_pages: dict[int, list[dict[str, Any]]],
    structure_analysis: dict[str, Any],
    page_annotations: dict[int, list[dict[str, Any]]],
    *,
    total_pages: int,
    title: str,
    language: str = "en",
    author: str | None = None,
) -> DocumentModel:
    """Assemble a DocumentModel from checkpoint data.

    Parameters
    ----------
    cleaned_pages:
        Mapping of page number to a list of cleaned paragraph dicts.
        Each paragraph dict must have at least ``paragraph_index`` and ``text``.
    structure_analysis:
        The document-level structure analysis dict as written by
        ``detect_document_structure`` (contains ``chapters`` list).
    page_annotations:
        Mapping of page number to a list of structure annotation dicts
        as written by ``detect_document_structure``.
    total_pages:
        Total number of pages in the source document.
    title:
        The document title for EPUB metadata.
    language:
        BCP 47 language tag (e.g. ``"nl"``, ``"en"``).
    author:
        Optional author name for EPUB metadata.
    """
    if total_pages < 1:
        raise ValueError("total_pages must be at least 1")

    metadata = EpubMetadata(
        title=title,
        language=language,
        author=author,
    )

    # Build a lookup: (page_number, paragraph_index) -> annotation
    annotation_lookup: dict[tuple[int, int], dict[str, Any]] = {}
    for page_number, annotations in page_annotations.items():
        for annotation in annotations:
            key = (page_number, annotation.get("paragraph_index", -1))
            annotation_lookup[key] = annotation

    # Determine chapter split points from the structure analysis.
    # Each entry in ``chapters`` has page_number, paragraph_index and title.
    chapter_splits = structure_analysis.get("chapters", [])

    # Build a set of (page_number, paragraph_index) that start a new chapter.
    chapter_start_keys: dict[tuple[int, int], str] = {}
    for chapter_info in chapter_splits:
        key = (chapter_info["page_number"], chapter_info["paragraph_index"])
        chapter_start_keys[key] = chapter_info["title"]

    # Walk all pages in order and distribute paragraphs into chapters.
    document_chapters: list[DocumentChapter] = []
    current_chapter: DocumentChapter | None = None

    for page_number in range(1, total_pages + 1):
        paragraphs = cleaned_pages.get(page_number, [])
        for paragraph in paragraphs:
            p_index = paragraph.get("paragraph_index")
            if p_index is None:
                # Paragraphs without an index are body text.
                p_index = -1
            text = _paragraph_text(paragraph)
            if not text:
                continue

            key = (page_number, p_index)

            # Check if this paragraph starts a new chapter.
            if key in chapter_start_keys:
                chapter_title = chapter_start_keys[key]
                current_chapter = DocumentChapter(title=chapter_title)
                document_chapters.append(current_chapter)
                # The chapter heading text itself is used as the chapter title,
                # so we do not also emit it as a body paragraph.
                continue

            # Check if this paragraph is a non-chapter heading.
            annotation = annotation_lookup.get(key)
            if annotation is not None and not annotation.get("chapter_start", False):
                kind = _heading_kind(annotation)
                block = DocumentParagraph(
                    text=text, kind=kind, source_page=page_number,
                    paragraph_index=p_index, bbox=_paragraph_bbox(paragraph),
                )
            else:
                block = DocumentParagraph(
                    text=text, kind="p", source_page=page_number,
                    paragraph_index=p_index, bbox=_paragraph_bbox(paragraph),
                )

            # If no chapter has been started yet, create a fallback.
            if current_chapter is None:
                current_chapter = DocumentChapter(title=title)
                document_chapters.append(current_chapter)

            current_chapter.paragraphs.append(block)

    # Edge case: if there are no paragraphs at all, create one empty chapter
    # so the EPUB builder does not reject the book.
    if not document_chapters:
        document_chapters.append(
            DocumentChapter(
                title=title,
                paragraphs=[DocumentParagraph(text="[Empty document]", kind="p")],
            )
        )

    # If the only chapter is the fallback and we detected display headings
    # but no explicit chapter labels, promote display_heading annotations
    # to chapter boundaries for better EPUB navigation.
    if (
        len(document_chapters) == 1
        and not chapter_splits
        and structure_analysis.get("heading_count", 0) > 0
    ):
        document_chapters = _promote_headings_to_chapters(
            document_chapters[0], page_annotations, title
        )

    return DocumentModel(metadata=metadata, chapters=document_chapters)


def _promote_headings_to_chapters(
    single_chapter: DocumentChapter,
    page_annotations: dict[int, list[dict[str, Any]]],
    fallback_title: str,
) -> list[DocumentChapter]:
    """Split a single chapter at display/section headings for better navigation.

    This is only called when the structure detector found headings but none of
    them were explicit chapter labels (e.g. a book that uses "PART ONE" style
    uppercase headings instead of "Chapter 1").  Each heading becomes a chapter
    boundary; paragraphs before the first heading stay in a preamble chapter.
    """
    # Collect all heading texts that appear in annotations.
    heading_texts: set[str] = set()
    for annotations in page_annotations.values():
        for annotation in annotations:
            if annotation.get("kind") in ("display_heading", "section_heading"):
                clean = " ".join(annotation.get("text", "").split()).strip()
                if clean:
                    heading_texts.add(clean)

    if not heading_texts:
        return [single_chapter]

    chapters: list[DocumentChapter] = []
    current = DocumentChapter(title=fallback_title)

    for paragraph in single_chapter.paragraphs:
        if paragraph.kind in ("h2", "h3") and paragraph.text in heading_texts:
            # Start a new chapter at this heading.
            if current.paragraphs:
                chapters.append(current)
            current = DocumentChapter(title=paragraph.text)
        else:
            current.paragraphs.append(paragraph)

    if current.paragraphs or not chapters:
        chapters.append(current)

    return chapters
