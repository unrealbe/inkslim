"""Language selection and conservative per-page language detection."""
from __future__ import annotations
from dataclasses import dataclass
from langdetect import DetectorFactory, LangDetectException, detect_langs

# Language identification remains diagnostic metadata; OCR always uses the explicit job selection.
DetectorFactory.seed = 0
_SELECTIONS = {
    "nld": ("nld",), "eng": ("eng",),
    "nld+eng": ("nld", "eng"), "fra": ("fra",), "deu": ("deu",),
    "fra+eng": ("fra", "eng"),
}
_EPUB_LANGUAGES = {
    "nld": "nl", "eng": "en", "nld+eng": "nl",
    "fra": "fr", "deu": "de", "fra+eng": "fr",
}
_DETECTED = {"nl": "nld", "en": "eng", "fr": "fra", "de": "deu"}
_MINIMUM_LETTERS = 80
_MINIMUM_PROBABILITY = 0.80

@dataclass(frozen=True, slots=True)
class LanguageDetection:
    language: str
    probability: float
    def to_dict(self):
        return {"language": self.language, "probability": round(self.probability, 4)}

def normalize_language_selection(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("language must be a supported language selection")
    selection = value.strip().lower()
    if selection not in _SELECTIONS:
        raise ValueError("language must be nld, eng, nld+eng, fra, deu or fra+eng")
    return selection

def ocr_languages_for_selection(value: str) -> list[str]:
    return list(_SELECTIONS[normalize_language_selection(value)])

def epub_language_for_selection(value: str) -> str:
    """Return a standard BCP 47 language code for EPUB metadata."""
    return _EPUB_LANGUAGES[normalize_language_selection(value)]

def detect_page_language(text: str) -> LanguageDetection | None:
    """Detect a dominant supported language; short/uncertain/mixed text returns None."""
    if not isinstance(text, str) or sum(ch.isalpha() for ch in text) < _MINIMUM_LETTERS:
        return None
    try:
        candidates = detect_langs(text)
    except LangDetectException:
        return None
    if not candidates:
        return None
    best = candidates[0]
    language = _DETECTED.get(best.lang)
    if language is None or best.prob < _MINIMUM_PROBABILITY:
        return None
    return LanguageDetection(language, best.prob)


def infer_document_language(
    pages: dict[int, str],
    *,
    fallback_selection: str,
    minimum_page_letters: int = 80,
    minimum_supporting_pages: int = 2,
    minimum_vote_share: float = 0.60,
) -> tuple[str, dict[str, object]]:
    """Infer EPUB language from reconstructed page text, without changing OCR selection."""
    fallback = epub_language_for_selection(fallback_selection)
    weighted: dict[str, int] = {}
    supporting_pages: dict[str, int] = {}
    evaluated_pages = 0

    for text in pages.values():
        if not isinstance(text, str):
            continue
        letters = sum(character.isalpha() for character in text)
        if letters < minimum_page_letters:
            continue
        evaluated_pages += 1
        detection = detect_page_language(text)
        if detection is None:
            continue
        language = _EPUB_LANGUAGES[detection.language]
        weighted[language] = weighted.get(language, 0) + letters
        supporting_pages[language] = supporting_pages.get(language, 0) + 1

    total_weight = sum(weighted.values())
    if not total_weight:
        return fallback, {
            "schema_version": 1,
            "method": "fallback_to_ocr_selection",
            "language": fallback,
            "evaluated_pages": evaluated_pages,
            "supporting_pages": 0,
            "vote_share": None,
        }

    candidate = max(weighted, key=weighted.get)
    vote_share = weighted[candidate] / total_weight
    page_support = supporting_pages[candidate]
    accepted = (
        page_support >= minimum_supporting_pages
        and vote_share >= minimum_vote_share
    )
    selected = candidate if accepted else fallback
    return selected, {
        "schema_version": 1,
        "method": (
            "weighted_reconstructed_page_language"
            if accepted else "fallback_to_ocr_selection"
        ),
        "language": selected,
        "detected_candidate": candidate,
        "evaluated_pages": evaluated_pages,
        "supporting_pages": page_support,
        "vote_share": round(vote_share, 4),
    }
