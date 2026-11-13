"""Conservative document-level stitching of paragraphs split by PDF page breaks."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

SCHEMA_VERSION = 1
_TERMINAL = re.compile(r"[.!?…][\"'’”)]*$")


def _bbox(paragraph: dict[str, Any]) -> tuple[float, float, float, float] | None:
    value = paragraph.get("bbox")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = box
    if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
        return None
    return box


def _can_stitch(
    previous: dict[str, Any],
    following: dict[str, Any],
    previous_page: dict[str, Any],
    following_page: dict[str, Any],
) -> bool:
    previous_text = previous.get("text")
    following_text = following.get("text")
    previous_box = _bbox(previous)
    following_box = _bbox(following)
    if not isinstance(previous_text, str) or not isinstance(following_text, str):
        return False
    previous_text = previous_text.strip()
    following_text = following_text.strip()
    if not previous_text or not following_text:
        return False
    if _TERMINAL.search(previous_text):
        return False
    if not following_text[0].islower():
        return False
    if previous_box is None or following_box is None:
        return False
    # Require a clear page-edge continuation rather than joining arbitrary
    # fragments that happen to be adjacent in reading order.
    if previous_box[3] < 0.55 or following_box[1] > 0.45:
        return False
    # Image-heavy, empty, or non-text pages are hard boundaries.
    for page in (previous_page, following_page):
        image_count = page.get("image_count")
        if page.get("decision") == "empty" or type(image_count) is not int or image_count != 0:
            return False
    return True


def stitch_page_paragraphs(
    cleaned_pages: dict[int, list[dict[str, Any]]],
    page_context: dict[int, dict[str, Any]],
    *,
    total_pages: int,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    """Join only high-confidence sentence continuations across adjacent pages.

    Merged paragraphs remain anchored to the page and paragraph index of their
    first fragment. Cross-page geometry is discarded because the two bboxes
    use different page coordinate systems. Input checkpoints are never mutated.
    """
    if total_pages < 1:
        raise ValueError("total_pages must be at least 1")
    if any(type(page) is not int or page < 1 or page > total_pages for page in cleaned_pages):
        raise ValueError("page number is outside the document")

    pages = {page: deepcopy(cleaned_pages.get(page, [])) for page in range(1, total_pages + 1)}
    stitched: list[dict[str, Any]] = []
    carry_root: dict[str, Any] | None = None
    carry_fragment: dict[str, Any] | None = None
    for page_number in range(1, total_pages):
        current = pages[page_number]
        following = pages[page_number + 1]
        if not following:
            carry_root = None
            carry_fragment = None
            continue
        if current:
            first = current[-1]
            boundary_fragment = first
        elif carry_root is not None and carry_fragment is not None:
            first = carry_root
            boundary_fragment = carry_fragment
        else:
            continue
        if _can_stitch(
            boundary_fragment, following[0],
            page_context.get(page_number, {}),
            page_context.get(page_number + 1, {}),
        ):
            second = following.pop(0)
            first_text = str(first["text"]).strip()
            second_text = str(second["text"]).strip()
            first["text"] = f"{first_text} {second_text}"
            first["bbox"] = None
            first["confidence"] = (
                (float(first["confidence"]) + float(second["confidence"])) / 2
                if isinstance(first.get("confidence"), (int, float))
                and isinstance(second.get("confidence"), (int, float))
                else first.get("confidence")
            )
            if first.get("source") != second.get("source"):
                first["source"] = "mixed"
            first["line_indices"] = list(first.get("line_indices", [])) + list(
                second.get("line_indices", [])
            )
            stitched.append({
                "from_page": page_number,
                "to_page": page_number + 1,
                "paragraph_index": first.get("paragraph_index"),
            })
            carry_root = first
            carry_fragment = second
        else:
            carry_root = None
            carry_fragment = None

    analysis = {
        "schema_version": SCHEMA_VERSION,
        "total_pages": total_pages,
        "stitched_count": len(stitched),
        "stitched": stitched,
    }
    return pages, analysis
