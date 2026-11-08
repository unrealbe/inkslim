from conversion.hyphenation import detect_visible_hyphenation_candidates
from conversion.lines import ReconstructedLine


def line(index, text):
    return ReconstructedLine(
        line_index=index, text=text, bbox=(0.1, 0.1 * index, 0.9, 0.1 * index + 0.03),
        confidence=90, source="tesseract", source_line_number=index,
        source_paragraph_number=1,
    )


def test_detects_visible_line_end_hyphen_candidate_without_changing_text():
    lines = [line(1, "informa-"), line(2, "tie verwerken")]
    candidates = detect_visible_hyphenation_candidates(lines)
    assert len(candidates) == 1
    assert candidates[0]["suggested_join"] == "informatie"
    assert candidates[0]["status"] == "review_required"
    assert lines[0].text == "informa-"
    assert lines[1].text == "tie verwerken"


def test_does_not_flag_real_compound_word_inside_line():
    assert detect_visible_hyphenation_candidates([
        line(1, "e-mail is handig"), line(2, "maar niet altijd")
    ]) == []


def test_requires_lowercase_continuation_and_minimum_fragment_length():
    assert detect_visible_hyphenation_candidates([
        line(1, "Een-"), line(2, "Nieuwe alinea")
    ]) == []
    assert detect_visible_hyphenation_candidates([
        line(1, "ab-"), line(2, "cd vervolg")
    ]) == []


def test_supports_unicode_hyphen_but_not_dash_punctuation():
    candidates = detect_visible_hyphenation_candidates([
        line(1, "samen‐"), line(2, "werken")
    ])
    assert len(candidates) == 1
    assert detect_visible_hyphenation_candidates([
        line(1, "gedachte—"), line(2, "vervolg")
    ]) == []
