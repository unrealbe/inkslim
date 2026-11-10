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
