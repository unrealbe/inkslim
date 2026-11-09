"""Diagnostic comparison of recognized page text and reconstructed paragraphs."""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

_TOKEN_RE = re.compile(r"[^\\W_]+(?:['’\\-][^\\W_]+)*", re.UNICODE)
_HYPHENATED_LINE_BREAK_RE = re.compile(r"(?<=\\w)[-\\u00ad]\\s*\\n\\s*(?=\\w)", re.UNICODE)


def _tokens(text: str) -> list[str]:
    """Tokenize text conservatively and join words split by line-end hyphens."""
    normalized = _HYPHENATED_LINE_BREAK_RE.sub("", text)
    return [token.casefold() for token in _TOKEN_RE.findall(normalized)]


def compare_text_fidelity(source_text: str, reconstructed_text: str) -> dict[str, Any]:
    """Compare token retention and order without returning any recognized text."""
    source_tokens = _tokens(source_text)
    reconstructed_tokens = _tokens(reconstructed_text)
    source_count = len(source_tokens)
    reconstructed_count = len(reconstructed_tokens)

    if source_count == 0:
        return {
            "status": "no_source_text",
            "source_token_count": 0,
            "reconstructed_token_count": reconstructed_count,
            "retained_token_count": 0,
            "token_retention": None,
            "sequence_similarity": None,
        }

    retained = sum((Counter(source_tokens) & Counter(reconstructed_tokens)).values())
    return {
        "status": "compared" if reconstructed_count else "no_reconstructed_text",
        "source_token_count": source_count,
        "reconstructed_token_count": reconstructed_count,
        "retained_token_count": retained,
        "token_retention": round(retained / source_count, 4),
        "sequence_similarity": round(
            SequenceMatcher(None, source_tokens, reconstructed_tokens).ratio(), 4
        ),
    }


def summarize_text_fidelity(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate page diagnostics, weighted by source token count."""
    source_count = sum(int(page["source_token_count"]) for page in pages)
    reconstructed_count = sum(int(page["reconstructed_token_count"]) for page in pages)
    retained_count = sum(int(page["retained_token_count"]) for page in pages)
    similarities = [
        float(page["sequence_similarity"])
        for page in pages
        if page.get("sequence_similarity") is not None
    ]
    return {
        "status": "available" if source_count else "not_available",
        "method": "casefolded_word_tokens_with_line_end_hyphen_join",
        "pages_compared": len(pages),
        "source_token_count": source_count,
        "reconstructed_token_count": reconstructed_count,
        "retained_token_count": retained_count,
        "token_retention": round(retained_count / source_count, 4) if source_count else None,
        "mean_sequence_similarity": (
            round(sum(similarities) / len(similarities), 4) if similarities else None
        ),
    }
