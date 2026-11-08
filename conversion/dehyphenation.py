"""Conservative dehyphenation of explicit discretionary soft hyphens.

A visible hyphen-minus at a line boundary is ambiguous: it may be a real
compound-word hyphen. Without language-aware lexical evidence, preserve it.
The Unicode soft hyphen (U+00AD), by contrast, explicitly marks a
discretionary break and can safely be removed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


SOFT_HYPHEN = "\u00ad"


def dehyphenate_paragraphs(
    paragraphs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Remove explicit discretionary soft hyphens, preserving all other text.

    Returns a new paragraph collection and the number of removed characters.
    The input is not mutated, making the operation safe to repeat on resume.
    """
    processed = deepcopy(paragraphs)
    removed = 0
    for paragraph in processed:
        text = paragraph.get("text")
        if not isinstance(text, str):
            raise ValueError("Paragraph text must be a string")
        count = text.count(SOFT_HYPHEN)
        if count:
            paragraph["text"] = text.replace(SOFT_HYPHEN, "")
            removed += count
    return processed, removed
