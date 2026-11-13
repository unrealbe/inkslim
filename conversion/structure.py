"""Conservative document structure detection from cleaned paragraphs.

Only explicit chapter labels, unambiguous numbered headings and short
standalone all-caps headings are annotated. Source paragraph text is never
rewritten; the annotations are persisted separately.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

SCHEMA_VERSION = 2
_CHAPTER = re.compile(
    r"^(?:chapter|hoofdstuk|chapitre|kapitel|deel|part|partie|teil)\s+"
    r"(?:\d{1,3}|[ivxlcdm]{1,12}|[a-z]+)"
    r"(?:\s*[:.\-–—]\s*.+)?$",
    re.IGNORECASE,
)
_SECTION = {
    "prologue", "epilogue", "foreword", "afterword", "preface",
    "introduction", "conclusion", "contents", "voorwoord", "nawoord",
    "inleiding", "besluit", "inhoud",
}
_NUMBERED = re.compile(r"^(\d{1,3}(?:\.\d{1,2}){0,2})\s+(.{2,75})$")
_TERMINAL = re.compile(r"[.!?…]$")
_TOC_LABELS = {
    "contents", "table of contents", "content", "inhoud", "inhoudsopgave",
    "sommaire", "table des matieres", "inhalt", "inhaltsverzeichnis", "indice",
}
_TOC_ENTRY = re.compile(
    r"^.{3,100}?(?:\\.{2,}|…+|\\s{2,})\\s*(?:\\d{1,4}|[ivxlcdm]{1,8})$",
    re.IGNORECASE,
)
_COPYRIGHT_SIGNALS = (
    "copyright", "all rights reserved", "isbn", "published by",
    "first published", "edition", "©",
)


def _page_role(
    paragraphs: list[dict[str, Any]], *, page_number: int, total_pages: int
) -> tuple[str | None, str | None]:
    """Classify only highly recognizable early contents/copyright pages.

    These pages are omitted from the reading flow because EPUB navigation is
    generated from the detected structure. Ambiguous pages remain untouched.
    """
    early_page_limit = min(10, max(1, (total_pages + 4) // 5))
    if page_number > early_page_limit:
        return None, None

    texts = [
        " ".join(str(item.get("text", "")).split()).strip()
        for item in paragraphs
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    texts = [item for item in texts if item]
    if not texts:
        return None, None

    normalized = {_normalize(item).strip(" :.-") for item in texts}
    has_contents_label = bool(normalized & _TOC_LABELS)
    toc_entries = sum(
        1 for item in texts
        if len(item) <= 120 and not _TERMINAL.search(item)
        and _TOC_ENTRY.fullmatch(item)
    )
    if (has_contents_label and toc_entries >= 2) or (
        toc_entries >= 4 and toc_entries / len(texts) >= 0.6
    ):
        return "contents", "contents_heading_and_page_numbered_entries" if has_contents_label else "repeated_page_numbered_entries"

    joined = " ".join(texts).casefold()
    signals = {signal for signal in _COPYRIGHT_SIGNALS if signal in joined}
    word_count = sum(len(item.split()) for item in texts)
    if word_count <= 120 and (
        len(signals) >= 2
        or ("all rights reserved" in signals and "isbn" in signals)
    ):
        return "copyright", "multiple_copyright_metadata_signals"

    return None, None


def _bbox(paragraph: dict[str, Any]) -> tuple[float, float, float, float] | None:
    value = paragraph.get("bbox")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = box
    return box if 0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1 else None


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _classify(paragraph: dict[str, Any]) -> dict[str, Any] | None:
    text = paragraph.get("text")
    box = _bbox(paragraph)
    if not isinstance(text, str) or box is None:
        return None
    clean = " ".join(text.split()).strip()
    normalized = _normalize(clean)
    if not 3 <= len(clean) <= 100 or _TERMINAL.search(clean) or box[1] > 0.82 or box[3] < 0.02:
        return None

    if _CHAPTER.fullmatch(normalized):
        kind, confidence, reason, chapter_start = (
            "chapter_heading", 0.98, "explicit_multilingual_chapter_label", True
        )
    elif normalized in _SECTION:
        kind, confidence, reason, chapter_start = (
            "section_heading", 0.94, "recognized_standalone_section_label", False
        )
    else:
        numbered = _NUMBERED.fullmatch(clean)
        if numbered:
            number = numbered.group(1)
            kind = "chapter_heading" if "." not in number else "section_heading"
            confidence, reason, chapter_start = 0.88, "standalone_numbered_heading", "." not in number
        else:
            letters = [character for character in clean if character.isalpha()]
            words = clean.split()
            if len(letters) < 4 or len(words) > 10:
                return None
            if sum(character.isupper() for character in letters) / len(letters) < 0.85:
                return None
            kind, confidence, reason, chapter_start = (
                "display_heading", 0.78, "short_standalone_all_caps_text", False
            )

    paragraph_index = paragraph.get("paragraph_index")
    if type(paragraph_index) is not int or paragraph_index < 0:
        return None
    return {
        "paragraph_index": paragraph_index,
        "text": clean,
        "kind": kind,
        "chapter_start": chapter_start,
        "confidence": confidence,
        "reason": reason,
        "bbox": list(box),
    }


def detect_document_structure(
    pages: dict[int, list[dict[str, Any]]], *, total_pages: int
) -> tuple[dict[str, Any], dict[int, list[dict[str, Any]]]]:
    """Return document-level chapter index and separate per-page annotations."""
    if total_pages < 1:
        raise ValueError("total_pages must be at least 1")
    if any(type(page) is not int or page < 1 or page > total_pages for page in pages):
        raise ValueError("page number is outside the document")

    annotations: dict[int, list[dict[str, Any]]] = {}
    chapters: list[dict[str, Any]] = []
    heading_count = 0
    page_roles: dict[str, str] = {}
    excluded_pages: list[int] = []
    exclusion_reasons: dict[str, str] = {}
    for page_number in range(1, total_pages + 1):
        page_paragraphs = pages.get(page_number, [])
        role, reason = _page_role(
            page_paragraphs, page_number=page_number, total_pages=total_pages
        )
        if role is not None:
            page_roles[str(page_number)] = role
            excluded_pages.append(page_number)
            if reason:
                exclusion_reasons[str(page_number)] = reason
            annotations[page_number] = []
            continue
        page_annotations = []
        for paragraph in page_paragraphs:
            annotation = _classify(paragraph)
            if annotation is None:
                continue
            heading_count += 1
            if annotation["chapter_start"]:
                chapter = {
                    "chapter_index": len(chapters) + 1,
                    "page_number": page_number,
                    "paragraph_index": annotation["paragraph_index"],
                    "title": annotation["text"],
                    "confidence": annotation["confidence"],
                    "reason": annotation["reason"],
                }
                chapters.append(chapter)
                annotation["chapter_index"] = chapter["chapter_index"]
            page_annotations.append(annotation)
        annotations[page_number] = page_annotations

    analysis = {
        "schema_version": SCHEMA_VERSION,
        "total_pages": total_pages,
        "page_roles": page_roles,
        "excluded_pages": excluded_pages,
        "exclusion_reasons": exclusion_reasons,
        "heading_count": heading_count,
        "chapter_count": len(chapters),
        "chapters": chapters,
    }
    return analysis, annotations
