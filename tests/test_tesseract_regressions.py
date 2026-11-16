"""Regression tests for the Tesseract adapter that need no Tesseract binary (mocked subprocess)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from conversion.lines import reconstruct_lines
from conversion.ocr.tesseract import OcrError, TesseractOcrEngine
from conversion.paragraphs import reconstruct_paragraphs

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def row(text, *, block=1, par=1, line=1, word=1, left=10, top=20, width=40, height=12, conf="95.0"):
    return f"5\t1\t{block}\t{par}\t{line}\t{word}\t{left}\t{top}\t{width}\t{height}\t{conf}\t{text}\n"


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "page.png"
    Image.new("RGB", (400, 300), "white").save(path)
    return path


def fake_run(monkeypatch, stdout="", *, returncode=0, stderr=""):
    completed = subprocess.CompletedProcess(["tesseract"], returncode, stdout=stdout, stderr=stderr)
    mocked = Mock(return_value=completed)
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)
    return mocked


# -- the double-quote bug: dialogue lines start with a quote character -----------------------------
def test_words_starting_or_ending_with_double_quotes_do_not_corrupt_the_page(image, monkeypatch):
    tsv = (
        HEADER
        + row('"Wat', line=1, word=1, left=10, top=20)
        + row('doe', line=1, word=2, left=60, top=20)
        + row('je?"', line=1, word=3, left=100, top=20)
        + row('"Niets,"', line=2, word=1, left=10, top=50)
        + row('zei', line=2, word=2, left=80, top=50)
        + row('"ik', line=3, word=1, left=10, top=80)
        + row('keek"', line=3, word=2, left=60, top=80)
    )
    fake_run(monkeypatch, tsv)
    result = TesseractOcrEngine().recognize(image, ["nld"])
    assert [b.text for b in result.blocks] == ['"Wat doe je?"', '"Niets,"  zei'.replace("  ", " "), '"ik keek"']
    assert all("\t" not in b.text and "\n" not in b.text for b in result.blocks)


def test_a_single_unbalanced_quote_does_not_swallow_later_rows(image, monkeypatch):
    tsv = HEADER + row('"Hallo', line=1) + row("gewoon", line=2, top=40) + row("tekst", line=3, top=60)
    fake_run(monkeypatch, tsv)
    result = TesseractOcrEngine().recognize(image, ["nld"])
    assert [b.text for b in result.blocks] == ['"Hallo', "gewoon", "tekst"]


# -- line and paragraph numbers must be unique per page --------------------------------------------
def test_paragraph_and_line_numbers_are_unique_across_blocks(image, monkeypatch):
    tsv = (
        HEADER
        + row("Eerste", block=1, par=1, line=1, top=20)
        + row("alinea", block=1, par=1, line=2, top=40)
        + row("Tweede", block=2, par=1, line=1, top=200)
        + row("blok", block=2, par=1, line=2, top=220)
    )
    fake_run(monkeypatch, tsv)
    blocks = TesseractOcrEngine().recognize(image, ["nld"]).blocks
    assert [b.paragraph_number for b in blocks] == [1, 1, 2, 2]
    assert [b.line_number for b in blocks] == [1, 2, 3, 4]


def test_tightly_set_blocks_stay_separate_paragraphs_in_the_pipeline(image, monkeypatch):
    """Regression: Tesseract restarts par_num at 1 in every block. paragraphs.py treats differing
    source paragraph numbers as a clear boundary, so with the raw numbers a heading and two
    paragraphs in adjacent blocks were merged into one paragraph."""
    tsv = (
        HEADER
        + row("HOOFDSTUK", block=1, par=1, line=1, top=100, left=100, width=300, height=24)
        + row("De eerste regel van de eerste alinea loopt door.", block=2, par=1, line=1, top=132, left=100, width=300, height=24)
        + row("De tweede regel van dezelfde alinea gaat verder.", block=2, par=1, line=2, top=160, left=100, width=300, height=24)
        + row("Hier begint een nieuwe alinea in een ander blok.", block=3, par=1, line=1, top=190, left=100, width=300, height=24)
        + row("En die loopt nog een regel door.", block=3, par=1, line=2, top=218, left=100, width=300, height=24)
    )
    fake_run(monkeypatch, tsv)
    page = TesseractOcrEngine().recognize(image, ["nld"])
    paragraphs = reconstruct_paragraphs(reconstruct_lines(page))
    assert [paragraph.text for paragraph in paragraphs] == [
        "HOOFDSTUK",
        "De eerste regel van de eerste alinea loopt door. De tweede regel van dezelfde alinea gaat verder.",
        "Hier begint een nieuwe alinea in een ander blok. En die loopt nog een regel door.",
    ]


def test_paragraphs_inside_one_block_are_numbered_in_reading_order(image, monkeypatch):
    tsv = (
        HEADER
        + row("a", block=1, par=1, line=1, top=20)
        + row("b", block=1, par=1, line=2, top=40)
        + row("c", block=1, par=2, line=1, top=90)
    )
    fake_run(monkeypatch, tsv)
    blocks = TesseractOcrEngine().recognize(image, ["nld"]).blocks
    assert [b.paragraph_number for b in blocks] == [1, 1, 2]


# -- command line -------------------------------------------------------------------------------
def test_default_mode_detects_orientation_and_mode_is_configurable(image, monkeypatch):
    mocked = fake_run(monkeypatch, HEADER)
    TesseractOcrEngine().recognize(image, ["nld"])
    assert mocked.call_args.args[0][5:7] == ["--psm", "1"]
    TesseractOcrEngine(page_segmentation_mode=6).recognize(image, ["nld"])
    assert mocked.call_args.args[0][5:7] == ["--psm", "6"]


@pytest.mark.parametrize("mode", [-1, 14, "3", None])
def test_invalid_page_segmentation_mode_is_rejected(mode):
    with pytest.raises(ValueError):
        TesseractOcrEngine(page_segmentation_mode=mode)


# -- runtime check (job-level failures) -----------------------------------------------------------
LIST_OK = "List of available languages in \"/usr/share/tesseract-ocr/5/tessdata/\" (4):\neng\nnld\nosd\ndeu\n"


def test_check_runtime_returns_installed_languages(monkeypatch):
    fake_run(monkeypatch, LIST_OK)
    assert TesseractOcrEngine().check_runtime(["nld", "eng"]) == {"eng", "nld", "osd", "deu"}


def test_check_runtime_accepts_a_list_printed_on_stderr(monkeypatch):
    fake_run(monkeypatch, "", stderr=LIST_OK)
    assert "nld" in TesseractOcrEngine().check_runtime(["nld"])


def test_missing_language_is_a_job_level_error(monkeypatch):
    fake_run(monkeypatch, LIST_OK)
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine().check_runtime(["nld", "fra"])
    assert error.value.code == "OCR_LANGUAGE_UNAVAILABLE"
    assert "fra" in error.value.public_message
    assert error.value.job_level


def test_missing_osd_data_is_detected_only_when_the_mode_needs_it(monkeypatch):
    fake_run(monkeypatch, "List:\neng\nnld\n")
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine().check_runtime(["nld"])
    assert error.value.code == "OCR_OSD_UNAVAILABLE" and error.value.job_level
    assert TesseractOcrEngine(page_segmentation_mode=3).check_runtime(["nld"]) == {"eng", "nld"}


def test_engine_not_installed_is_a_job_level_error(monkeypatch):
    monkeypatch.setattr(
        "conversion.ocr.tesseract.subprocess.run", Mock(side_effect=FileNotFoundError("tesseract"))
    )
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine().check_runtime(["nld"])
    assert error.value.code == "OCR_ENGINE_UNAVAILABLE" and error.value.job_level


def test_engine_exit_error_during_runtime_check(monkeypatch):
    fake_run(monkeypatch, "", returncode=1, stderr="boom")
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine().check_runtime(["nld"])
    assert error.value.code == "OCR_ENGINE_UNAVAILABLE"


def test_invalid_language_is_rejected_by_the_runtime_check(monkeypatch):
    fake_run(monkeypatch, LIST_OK)
    with pytest.raises(ValueError):
        TesseractOcrEngine().check_runtime(["nld; rm -rf /"])


@pytest.mark.parametrize("code", ["OCR_TIMEOUT", "OCR_FAILED", "OCR_INVALID_OUTPUT", "OCR_INVALID_IMAGE"])
def test_page_level_errors_are_not_job_level(code):
    assert not OcrError(code, "x").job_level
