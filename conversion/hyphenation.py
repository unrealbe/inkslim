"""Detection of possible visible hyphenation at reconstructed line boundaries.

Candidates are advisory only. A visible hyphen can be part of a legitimate
compound, so this stage never changes recognized text automatically.
"""
from __future__ import annotations

import re
from .lines import ReconstructedLine

# Restrict matching to ordinary hyphen-minus and Unicode hyphen. Do not treat
# en/em dashes, non-breaking hyphens or mathematical minus signs as word breaks.
_ENDING = re.compile(r"(?P<fragment>[^\W\d_]{3,})[-\u2010]$", re.UNICODE)
_STARTING = re.compile(r"^[a-zà-öø-ÿ][^\W\d_]{1,}", re.UNICODE)


def detect_visible_hyphenation_candidates(
    lines: list[ReconstructedLine],
) -> list[dict[str, object]]:
    """Return likely line-wrap candidates without changing source text.

    Detection requires a sufficiently long alphabetic fragment, a hyphen at
    the physical line end and a lowercase alphabetic continuation on the next
    line. These signals are not proof: candidates remain flagged for review.
    """
    candidates: list[dict[str, object]] = []
    for previous, current in zip(lines, lines[1:]):
        previous_text = previous.text.rstrip()
        current_text = current.text.lstrip()
        match = _ENDING.search(previous_text)
        if match is None or not _STARTING.match(current_text):
            continue
        fragment = match.group("fragment")
        continuation = _STARTING.match(current_text)
        assert continuation is not None
        candidates.append({
            "previous_line_index": previous.line_index,
            "next_line_index": current.line_index,
            "fragment": fragment,
            "continuation": continuation.group(0),
            "suggested_join": fragment + continuation.group(0),
            "original_left_text": previous.text,
            "original_right_text": current.text,
            "status": "review_required",
            "reason": "visible_line_end_hyphen_with_lowercase_continuation",
        })
    return candidates
