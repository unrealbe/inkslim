from __future__ import annotations

from conversion.text_fidelity import compare_text_fidelity, summarize_text_fidelity


def test_text_fidelity_joins_words_hyphenated_at_line_end():
    result = compare_text_fidelity(
        "The inter-\nnational road continues",
        "The international road continues",
    )

    assert result["status"] == "compared"
    assert result["source_token_count"] == 4
    assert result["reconstructed_token_count"] == 4
    assert result["retained_token_count"] == 4
    assert result["token_retention"] == 1.0
    assert result["sequence_similarity"] == 1.0


def test_token_retention_is_independent_of_sequence_order():
    result = compare_text_fidelity(
        "one two three four",
        "one three two four",
    )

    assert result["token_retention"] == 1.0
    assert result["sequence_similarity"] < 1.0


def test_text_fidelity_reports_missing_reconstruction_and_missing_source():
    missing_reconstruction = compare_text_fidelity("source text exists", "")
    missing_source = compare_text_fidelity("", "reconstructed text")

    assert missing_reconstruction["status"] == "no_reconstructed_text"
    assert missing_reconstruction["token_retention"] == 0.0
    assert missing_reconstruction["sequence_similarity"] == 0.0
    assert missing_source["status"] == "no_source_text"
    assert missing_source["token_retention"] is None
    assert missing_source["sequence_similarity"] is None


def test_text_fidelity_summary_weights_token_retention_by_source_tokens():
    first = compare_text_fidelity("one two three four", "one two three four")
    second = compare_text_fidelity("five six", "five")

    summary = summarize_text_fidelity([first, second])

    assert summary["status"] == "available"
    assert summary["pages_compared"] == 2
    assert summary["source_token_count"] == 6
    assert summary["reconstructed_token_count"] == 5
    assert summary["retained_token_count"] == 5
    assert summary["token_retention"] == round(5 / 6, 4)
    assert summary["mean_sequence_similarity"] == 0.8333
