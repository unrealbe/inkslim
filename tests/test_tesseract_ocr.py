from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from conversion.models import QualityStatus, TextSource
from conversion.ocr.tesseract import OcrError, TesseractOcrEngine


TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def word(*, block=1, paragraph=1, line=1, word_num=1, left=10, top=20,
         width=30, height=12, confidence="95.0", text="Hello"):
    return (
        f"5\t1\t{block}\t{paragraph}\t{line}\t{word_num}\t{left}\t{top}"
        f"\t{width}\t{height}\t{confidence}\t{text}\n"
    )


@pytest.fixture
def page_image(tmp_path: Path) -> Path:
    path = tmp_path / "page.png"
    Image.new("RGB", (200, 100), "white").save(path)
    return path


def run_tesseract(monkeypatch, stdout=TSV_HEADER, *, returncode=0, stderr=""):
    completed = subprocess.CompletedProcess(
        args=["tesseract"], returncode=returncode, stdout=stdout, stderr=stderr
    )
    mocked = Mock(return_value=completed)
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)
    return mocked


def test_parses_words_into_lines_and_normalizes_geometry(page_image, monkeypatch):
    mocked = run_tesseract(
        monkeypatch,
        TSV_HEADER
        + word(word_num=1, left=10, top=20, width=30, text="Hallo")
        + word(word_num=2, left=45, top=20, width=25, text="wereld")
        + word(block=1, paragraph=1, line=2, left=10, top=40, width=50, text="Tweede"),
    )

    result = TesseractOcrEngine().recognize(page_image, ["nld", "eng"], page_number=3)

    assert result.page_number == 3
    assert result.width == 200
    assert result.height == 100
    assert result.source is TextSource.TESSERACT
    assert result.quality is QualityStatus.HIGH
    assert [block.text for block in result.blocks] == ["Hallo wereld", "Tweede"]
    assert result.blocks[0].bbox == (0.05, 0.2, 0.35, 0.32)
    assert result.blocks[0].confidence == 95.0
    assert result.blocks[0].line_number == 1
    assert mocked.call_args.args[0] == [
        "tesseract", str(page_image), "stdout", "-l", "nld+eng", "--psm", "3", "tsv"
    ]
    assert mocked.call_args.kwargs["timeout"] == 120
    assert "shell" not in mocked.call_args.kwargs


def test_deduplicates_language_codes_and_supports_nld(page_image, monkeypatch):
    mocked = run_tesseract(monkeypatch)
    TesseractOcrEngine().recognize(page_image, ["nld", "nld", "eng"])
    assert mocked.call_args.args[0][4] == "nld+eng"


def test_empty_page_returns_review_without_blocks(page_image, monkeypatch):
    run_tesseract(monkeypatch)
    result = TesseractOcrEngine().recognize(page_image, ["eng"])
    assert result.blocks == []
    assert result.quality is QualityStatus.REVIEW
    assert result.metrics.mean_ocr_confidence is None
    assert result.warnings == ["OCR_NO_TEXT"]


def test_low_confidence_is_preserved_and_flagged(page_image, monkeypatch):
    run_tesseract(monkeypatch, TSV_HEADER + word(confidence="24.5", text="onzeker"))
    result = TesseractOcrEngine().recognize(page_image, ["eng"])
    assert result.blocks[0].confidence == 24.5
    assert result.metrics.mean_ocr_confidence == 24.5
    assert result.quality is QualityStatus.REVIEW
    assert "LOW_OCR_CONFIDENCE" in result.warnings


def test_negative_confidence_keeps_text_but_marks_confidence_unavailable(page_image, monkeypatch):
    run_tesseract(monkeypatch, TSV_HEADER + word(confidence="-1", text="tekst"))
    result = TesseractOcrEngine().recognize(page_image, ["nld"])
    assert result.blocks[0].text == "tekst"
    assert result.blocks[0].confidence is None
    assert result.metrics.mean_ocr_confidence is None
    assert "OCR_CONFIDENCE_UNAVAILABLE" in result.warnings


def test_timeout_is_translated_to_safe_error(page_image, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", timeout)
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine(timeout_seconds=7).recognize(page_image, ["eng"])
    assert error.value.code == "OCR_TIMEOUT"


def test_corrupt_image_is_rejected_before_subprocess(tmp_path, monkeypatch):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    mocked = Mock()
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine().recognize(path, ["eng"])
    assert error.value.code == "OCR_INVALID_IMAGE"
    mocked.assert_not_called()


def test_nonzero_exit_is_a_safe_error(page_image, monkeypatch):
    run_tesseract(monkeypatch, returncode=1, stderr="bad image")
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine().recognize(page_image, ["eng"])
    assert error.value.code == "OCR_FAILED"


def test_invalid_language_is_rejected(page_image, monkeypatch):
    mocked = Mock()
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)
    with pytest.raises(ValueError):
        TesseractOcrEngine().recognize(page_image, ["eng;cat"])
    mocked.assert_not_called()


def test_image_pixel_limit_is_enforced(page_image, monkeypatch):
    mocked = Mock()
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine(max_image_pixels=100).recognize(page_image, ["eng"])
    assert error.value.code == "OCR_IMAGE_LIMIT"
    mocked.assert_not_called()


def test_rotated_page_image_coordinates_are_kept_as_rendered(page_image, monkeypatch):
    run_tesseract(monkeypatch, TSV_HEADER + word(left=150, top=5, width=40, height=20))
    result = TesseractOcrEngine().recognize(page_image, ["eng"])
    assert result.blocks[0].bbox == (0.75, 0.05, 0.95, 0.25)


def test_malformed_tsv_is_rejected(page_image, monkeypatch):
    run_tesseract(monkeypatch, "unexpected\tcolumns\n5\tbroken\n")
    with pytest.raises(OcrError) as error:
        TesseractOcrEngine().recognize(page_image, ["eng"])
    assert error.value.code == "OCR_INVALID_OUTPUT"


def test_negative_and_out_of_bounds_boxes_are_ignored(page_image, monkeypatch):
    run_tesseract(
        monkeypatch,
        TSV_HEADER
        + word(left=-1, text="negatief")
        + word(left=190, width=30, text="rand"),
    )
    result = TesseractOcrEngine().recognize(page_image, ["eng"])
    assert [block.text for block in result.blocks] == ["rand"]
    assert result.blocks[0].bbox == (0.95, 0.2, 1.0, 0.32)



def test_low_confidence_retries_with_psm_6_and_selects_better_result(page_image, monkeypatch):
    first = subprocess.CompletedProcess(
        args=["tesseract"], returncode=0,
        stdout=TSV_HEADER + word(confidence="55.0", text="een twee drie vier vijf"),
        stderr="",
    )
    retry = subprocess.CompletedProcess(
        args=["tesseract"], returncode=0,
        stdout=TSV_HEADER + word(confidence="82.0", text="een twee drie vier vijf"),
        stderr="",
    )
    mocked = Mock(side_effect=[first, retry])
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)

    result = TesseractOcrEngine().recognize(page_image, ["nld"])

    assert result.metrics.mean_ocr_confidence == 82.0
    assert result.quality is QualityStatus.HIGH
    assert result.warnings == ["OCR_RETRY_PSM_6_SELECTED"]
    assert mocked.call_count == 2
    assert mocked.call_args_list[0].args[0][6] == "3"
    assert mocked.call_args_list[1].args[0][6] == "6"


def test_retry_is_rejected_when_it_loses_too_much_text(page_image, monkeypatch):
    first = subprocess.CompletedProcess(
        args=["tesseract"], returncode=0,
        stdout=TSV_HEADER + word(confidence="55.0", text="een twee drie vier vijf"),
        stderr="",
    )
    retry = subprocess.CompletedProcess(
        args=["tesseract"], returncode=0,
        stdout=TSV_HEADER + word(confidence="95.0", text="een"),
        stderr="",
    )
    mocked = Mock(side_effect=[first, retry])
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)

    result = TesseractOcrEngine().recognize(page_image, ["nld"])

    assert result.metrics.mean_ocr_confidence == 55.0
    assert result.blocks[0].text == "een twee drie vier vijf"
    assert result.quality is QualityStatus.REVIEW
    assert result.warnings == ["LOW_OCR_CONFIDENCE", "OCR_RETRY_PSM_6_NOT_SELECTED"]


def test_retry_failure_preserves_first_ocr_result(page_image, monkeypatch):
    first = subprocess.CompletedProcess(
        args=["tesseract"], returncode=0,
        stdout=TSV_HEADER + word(confidence="55.0", text="originele tekst"),
        stderr="",
    )
    mocked = Mock(side_effect=[first, subprocess.TimeoutExpired(["tesseract"], 120)])
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)

    result = TesseractOcrEngine().recognize(page_image, ["nld"])

    assert result.blocks[0].text == "originele tekst"
    assert result.metrics.mean_ocr_confidence == 55.0
    assert result.warnings == ["LOW_OCR_CONFIDENCE", "OCR_RETRY_PSM_6_FAILED"]



def test_retry_uses_only_remaining_page_timeout(page_image, monkeypatch):
    first = subprocess.CompletedProcess(
        args=["tesseract"], returncode=0,
        stdout=TSV_HEADER + word(confidence="55.0", text="een twee drie vier vijf"),
        stderr="",
    )
    retry = subprocess.CompletedProcess(
        args=["tesseract"], returncode=0,
        stdout=TSV_HEADER + word(confidence="82.0", text="een twee drie vier vijf"),
        stderr="",
    )
    mocked = Mock(side_effect=[first, retry])
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)
    monotonic = Mock(side_effect=[100.0, 108.0])
    monkeypatch.setattr("conversion.ocr.tesseract.time.monotonic", monotonic)

    result = TesseractOcrEngine(timeout_seconds=20).recognize(page_image, ["nld"])

    assert result.metrics.mean_ocr_confidence == 82.0
    assert mocked.call_args_list[0].kwargs["timeout"] == 20
    assert mocked.call_args_list[1].kwargs["timeout"] == 12.0


def test_retry_is_skipped_when_page_timeout_budget_is_exhausted(page_image, monkeypatch):
    first = subprocess.CompletedProcess(
        args=["tesseract"], returncode=0,
        stdout=TSV_HEADER + word(confidence="55.0", text="originele tekst"),
        stderr="",
    )
    mocked = Mock(return_value=first)
    monkeypatch.setattr("conversion.ocr.tesseract.subprocess.run", mocked)
    monotonic = Mock(side_effect=[100.0, 120.0])
    monkeypatch.setattr("conversion.ocr.tesseract.time.monotonic", monotonic)

    result = TesseractOcrEngine(timeout_seconds=20).recognize(page_image, ["nld"])

    assert result.blocks[0].text == "originele tekst"
    assert mocked.call_count == 1
    assert "OCR_RETRY_PSM_6_SKIPPED_TIMEOUT_BUDGET" in result.warnings
