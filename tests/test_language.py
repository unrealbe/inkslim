import pytest
from conversion.language import (
    detect_page_language,
    epub_language_for_selection,
    normalize_language_selection,
    ocr_languages_for_selection,
    infer_document_language,
)

@pytest.mark.parametrize(("selection", "expected"), [
    ("nld", ["nld"]), ("eng", ["eng"]),
    ("fra", ["fra"]), ("deu", ["deu"]),
])
def test_explicit_language_selection(selection, expected):
    assert ocr_languages_for_selection(selection) == expected

@pytest.mark.parametrize(("selection", "expected_epub"), [
    ("nld", "nl"), ("eng", "en"),
    ("fra", "fr"), ("deu", "de"),
])
def test_epub_language_for_selection(selection, expected_epub):
    assert epub_language_for_selection(selection) == expected_epub

@pytest.mark.parametrize("selection", ["", "nl", "english", "nld+deu", "nld+eng", "fra+eng", "auto", "auto+eng"])
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


def _english_page():
    return (
        "The application processes every page of the book and reconstructs "
        "the original paragraphs in their natural reading order. "
    ) * 3


def _french_page():
    return (
        "Le traitement reconstruit les paragraphes du livre dans leur ordre "
        "de lecture naturel et conserve le contenu du document. "
    ) * 3


def test_document_language_uses_reconstructed_text_not_ocr_selection():
    selected, diagnostics = infer_document_language(
        {1: _english_page(), 2: _english_page()},
        fallback_selection="eng",
    )
    assert selected == "en"
    assert diagnostics["method"] == "weighted_reconstructed_page_language"
    assert diagnostics["supporting_pages"] == 2


def test_document_language_falls_back_when_pages_are_too_short():
    selected, diagnostics = infer_document_language(
        {1: "Title", 2: "Advertisement"},
        fallback_selection="fra",
    )
    assert selected == "fr"
    assert diagnostics["method"] == "fallback_to_ocr_selection"
    assert diagnostics["evaluated_pages"] == 0


def test_document_language_falls_back_for_ambiguous_page_vote():
    selected, diagnostics = infer_document_language(
        {1: _english_page(), 2: _french_page()},
        fallback_selection="fra",
    )
    assert selected == "fr"
    assert diagnostics["method"] == "fallback_to_ocr_selection"
    assert diagnostics["vote_share"] <= 0.60


def test_short_title_and_advert_pages_do_not_outvote_body_text():
    selected, diagnostics = infer_document_language(
        {
            1: "A short title",
            2: _english_page(),
            3: _english_page(),
            4: "Advertisement",
        },
        fallback_selection="nld",
    )
    assert selected == "en"
    assert diagnostics["evaluated_pages"] == 2


def test_document_language_inference_keeps_explicit_single_ocr_language():
    assert ocr_languages_for_selection("eng") == ["eng"]
    assert ocr_languages_for_selection("nld") == ["nld"]
