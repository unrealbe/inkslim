"""Conservative, language-neutral quality checks for native PDF text."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import PageDecision

_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True, slots=True)
class NativeTextAssessment:
    character_count: int
    word_count: int
    meaningful_character_count: int
    meaningful_character_ratio: float
    control_character_ratio: float
    replacement_character_ratio: float
    single_character_word_ratio: float
    geometry_usable_ratio: float | None
    quality: float | None
    decision: PageDecision
    warnings: tuple[str, ...]


def _is_meaningful(character: str) -> bool:
    return unicodedata.category(character)[0] in {"L", "N", "P"}


def assess_native_text(
    text: str,
    *,
    image_count: int,
    geometry_usable_ratio: float | None,
    min_meaningful_characters: int = 20,
    suspicious_ratio_limit: float = 0.02,
    meaningful_ratio_floor: float = 0.70,
    single_character_word_limit: float = 0.35,
    geometry_ratio_floor: float = 0.80,
) -> NativeTextAssessment:
    """Classify native text without assuming a specific book language.

    Ratios use non-whitespace characters as the denominator. Tiny page-number-only
    layers on otherwise empty/vectorless pages are classified as EMPTY.
    """
    if image_count < 0:
        raise ValueError("image_count cannot be negative")
    if min_meaningful_characters < 1:
        raise ValueError("min_meaningful_characters must be positive")

    non_whitespace = [character for character in text if not character.isspace()]
    denominator = max(1, len(non_whitespace))
    meaningful = sum(_is_meaningful(character) for character in non_whitespace)
    controls = sum(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Cn"}
        or character == "\ufffd"
        for character in non_whitespace
    )
    replacements = text.count("\ufffd")
    words = _WORD_RE.findall(text)
    single_words = sum(len(word) == 1 for word in words)

    meaningful_ratio = meaningful / denominator
    control_ratio = controls / denominator
    replacement_ratio = replacements / denominator
    single_ratio = single_words / max(1, len(words))
    warnings: list[str] = []

    if not text.strip():
        if image_count:
            decision = PageDecision.OCR_REQUIRED
            warnings.append("IMAGE_WITHOUT_NATIVE_TEXT")
        else:
            decision = PageDecision.EMPTY
            warnings.append("NO_TEXT_OR_IMAGE_CONTENT")
        quality = None
    elif meaningful < min_meaningful_characters:
        if image_count:
            decision = PageDecision.OCR_REQUIRED
            warnings.append("INSUFFICIENT_NATIVE_TEXT_ON_IMAGE_PAGE")
        elif meaningful <= 8 and re.fullmatch(r"[\s\d\W_]*", text, re.UNICODE):
            decision = PageDecision.EMPTY
            warnings.append("PAGE_NUMBER_OR_TINY_NON_BODY_TEXT")
        else:
            decision = PageDecision.OCR_REQUIRED
            warnings.append("INSUFFICIENT_NATIVE_TEXT")
        quality = meaningful_ratio
    else:
        if control_ratio > suspicious_ratio_limit:
            warnings.append("HIGH_CONTROL_CHARACTER_RATIO")
        if replacement_ratio > suspicious_ratio_limit:
            warnings.append("HIGH_REPLACEMENT_CHARACTER_RATIO")
        if meaningful_ratio < meaningful_ratio_floor:
            warnings.append("LOW_MEANINGFUL_CHARACTER_RATIO")
        if single_ratio > single_character_word_limit:
            warnings.append("HIGH_SINGLE_CHARACTER_WORD_RATIO")
        if geometry_usable_ratio is None:
            warnings.append("TEXT_GEOMETRY_UNAVAILABLE")
        elif geometry_usable_ratio < geometry_ratio_floor:
            warnings.append("POOR_TEXT_GEOMETRY")

        score = meaningful_ratio
        score *= max(0.0, 1.0 - min(1.0, control_ratio * 10.0))
        score *= max(0.0, 1.0 - min(1.0, replacement_ratio * 10.0))
        score *= max(0.2, 1.0 - min(0.8, single_ratio * 0.8))
        if geometry_usable_ratio is not None:
            score *= 0.5 + (0.5 * geometry_usable_ratio)
        quality = min(1.0, max(0.0, score))
        decision = PageDecision.OCR_REQUIRED if warnings else PageDecision.USE_NATIVE

    return NativeTextAssessment(
        character_count=len(text),
        word_count=len(words),
        meaningful_character_count=meaningful,
        meaningful_character_ratio=meaningful_ratio,
        control_character_ratio=control_ratio,
        replacement_character_ratio=replacement_ratio,
        single_character_word_ratio=single_ratio,
        geometry_usable_ratio=geometry_usable_ratio,
        quality=quality,
        decision=decision,
        warnings=tuple(warnings),
    )
