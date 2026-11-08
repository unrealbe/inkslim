import pytest
from conversion.language import (
    detect_page_language,
    epub_language_for_selection,
    normalize_language_selection,
    ocr_languages_for_selection,
)

def test_auto_uses_dutch_and_english_models():
    assert normalize_language_selection("auto") == "auto"
    assert ocr_languages_for_selection("auto") == ["nld", "eng"]
    assert epub_language_for_selection("auto") == "nl"

@pytest.mark.parametrize(("selection", "expected"), [
    ("nld", ["nld"]), ("eng", ["eng"]), ("nld+eng", ["nld", "eng"]),
    ("fra", ["fra"]), ("deu", ["deu"]), ("fra+eng", ["fra", "eng"]),
])
def test_explicit_language_selection(selection, expected):
    assert ocr_languages_for_selection(selection) == expected

@pytest.mark.parametrize(("selection", "expected_epub"), [
    ("nld", "nl"), ("eng", "en"), ("nld+eng", "nl"),
    ("fra", "fr"), ("deu", "de"), ("fra+eng", "fr"),
])
def test_epub_language_for_selection(selection, expected_epub):
    assert epub_language_for_selection(selection) == expected_epub

@pytest.mark.parametrize("selection", ["", "nl", "english", "nld+deu", "auto+eng"])
def test_rejects_unsupported_language_selection(selection):
    with pytest.raises(ValueError):
        normalize_language_selection(selection)

def test_detects_dutch_and_english():
    dutch = ("Dit is een Nederlandse tekst over het verwerken van documenten. "
             "De toepassing herkent tekst op iedere pagina en bewaart de resultaten "
             "zodat een onderbroken conversie later veilig kan worden hervat. ") * 3
    english = ("This is an English text about processing documents. "
               "The application recognizes text on every page and stores the results "
               "so an interrupted conversion can safely resume at a later time. ") * 3
    assert detect_page_language(dutch).language == "nld"
    assert detect_page_language(english).language == "eng"

def test_short_text_does_not_force_language():
    assert detect_page_language("A short page of text.") is None
