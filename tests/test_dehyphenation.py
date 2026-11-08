from conversion.dehyphenation import dehyphenate_paragraphs


def test_removes_explicit_soft_hyphen_at_line_wrap():
    source = [{"paragraph_index": 1, "text": "infor\u00admatie verwerken"}]

    result, removed = dehyphenate_paragraphs(source)

    assert result[0]["text"] == "informatie verwerken"
    assert removed == 1
    assert source[0]["text"] == "infor\u00admatie verwerken"


def test_removes_multiple_soft_hyphens_and_is_idempotent():
    source = [{"text": "co\u00adöperatie en ver\u00adwerking"}]

    result, removed = dehyphenate_paragraphs(source)
    repeated, repeated_removed = dehyphenate_paragraphs(result)

    assert result[0]["text"] == "coöperatie en verwerking"
    assert removed == 2
    assert repeated == result
    assert repeated_removed == 0


def test_preserves_visible_hyphens_and_compound_words():
    source = [
        {"text": "e-mail"},
        {"text": "niet-roker"},
        {"text": "para- graaf"},
        {"text": "ISO-27001"},
    ]

    result, removed = dehyphenate_paragraphs(source)

    assert [item["text"] for item in result] == [
        "e-mail", "niet-roker", "para- graaf", "ISO-27001"
    ]
    assert removed == 0


def test_empty_input():
    assert dehyphenate_paragraphs([]) == ([], 0)


def test_rejects_invalid_paragraph_text():
    import pytest

    with pytest.raises(ValueError, match="Paragraph text"):
        dehyphenate_paragraphs([{"text": None}])
