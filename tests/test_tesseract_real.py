"""Tests that run the real Tesseract binary. Skipped when Tesseract or its language data is missing.

The mocked tests cannot catch how Tesseract really formats its output or how well it reads a page;
these do. CI installs tesseract-ocr with the eng, nld and osd data so they run there.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conversion.models import QualityStatus
from conversion.ocr.tesseract import TesseractOcrEngine
from ocr_fixtures import DUTCH_LINES, ENGLISH_LINES, accent_font_path, page_image


def _installed() -> set[str]:
    executable = shutil.which("tesseract")
    if not executable:
        return set()
    done = subprocess.run([executable, "--list-langs"], capture_output=True, text=True)
    return {line.strip() for line in (done.stdout + done.stderr).splitlines()[1:] if line.strip()}


INSTALLED = _installed()
pytestmark = pytest.mark.skipif(
    not {"eng", "nld", "osd"} <= INSTALLED, reason="needs Tesseract with eng, nld and osd data"
)

DIALOGUE = [
    '"Wat doe je daar?" vroeg haar vader zonder op te kijken.',
    '"Niets," zei Marie, "ik keek alleen even naar buiten."',
    '"Het sneeuwt," zei hij. "Dat zie ik zelf ook wel."',
    'Ze zweeg. "Misschien moeten we vandaag thuisblijven."',
    '"Ja," zei hij zacht, "misschien wel."',
    "Daarna werd het stil in de keuken en tikte alleen de klok.",
    '"Papa?" vroeg ze na een tijdje.',
    '"Ja?" zei hij, en eindelijk keek hij op.',
]


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def agreement(expected: list[str], result) -> float:
    got = words(" ".join(block.text for block in result.blocks))
    return difflib.SequenceMatcher(None, words(" ".join(expected)), got, autojunk=False).ratio()


@pytest.fixture
def ocr(tmp_path: Path):
    engine = TesseractOcrEngine(timeout_seconds=120)

    def run(image, languages=("nld",), **kwargs):
        path = tmp_path / "page.png"
        image.save(path, dpi=(200, 200))
        return engine.recognize(path, list(languages), **kwargs)

    return run


def test_runtime_check_passes_with_installed_data():
    assert {"nld", "eng", "osd"} <= TesseractOcrEngine().check_runtime(["nld", "eng"])


def test_dutch_page_is_read_line_by_line_with_valid_geometry(ocr):
    result = ocr(page_image(DUTCH_LINES))
    assert agreement(DUTCH_LINES, result) >= 0.97
    assert len(result.blocks) == len(DUTCH_LINES)
    assert result.quality is QualityStatus.HIGH
    assert [b.line_number for b in result.blocks] == list(range(1, len(DUTCH_LINES) + 1))
    for block in result.blocks:
        x0, y0, x1, y1 = block.bbox
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    tops = [b.bbox[1] for b in result.blocks]
    assert tops == sorted(tops)  # reading order is top to bottom


def test_dialogue_with_quotation_marks_is_not_corrupted(ocr):
    """Regression: csv quoting turned every dialogue page into 500+ bogus words."""
    result = ocr(page_image(DIALOGUE))
    assert len(result.blocks) == len(DIALOGUE)
    assert not any("\t" in b.text or "\n" in b.text for b in result.blocks)
    assert agreement(DIALOGUE, result) >= 0.95


def test_mixed_dutch_and_english_page(ocr):
    lines = DUTCH_LINES[:3] + ENGLISH_LINES[:3]
    result = ocr(page_image(lines), languages=("nld", "eng"))
    assert agreement(lines, result) >= 0.93


@pytest.mark.skipif(accent_font_path() is None, reason="needs a system font with accented letters")
def test_accented_characters_survive(ocr):
    lines = [
        "De naïeve coöperatie bood één kopje koffie aan, ook voor Zoë.",
        "Daarna aten ze crème brûlée in het café aan de overkant.",
    ]
    result = ocr(page_image(lines, accents=True, font_size=36))
    assert agreement(lines, result) >= 0.9
    text = " ".join(block.text for block in result.blocks).lower()
    assert sum(ch in text for ch in "ïöéëèûà") >= 4  # accented letters are kept, not stripped


def test_blank_page_is_reported_for_review_without_blocks(ocr):
    from PIL import Image

    result = ocr(Image.new("L", (1654, 2339), 245))
    assert result.blocks == []
    assert result.quality is QualityStatus.REVIEW
    assert "OCR_NO_TEXT" in result.warnings


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_pages_scanned_sideways_or_upside_down_are_still_read(ocr, angle):
    """Regression: with --psm 3 a sideways page was read as garbage (agreement 0.02)."""
    page = page_image(DUTCH_LINES).rotate(angle, expand=True)
    assert agreement(DUTCH_LINES, ocr(page)) >= 0.95


def test_multi_block_page_has_unique_paragraph_numbers(ocr):
    # Two well separated paragraphs: Tesseract reports them as separate paragraphs/blocks.
    first = DUTCH_LINES[:3]
    second = DUTCH_LINES[3:6]
    image = page_image(first + [""] * 5 + second)
    blocks = ocr(image).blocks
    numbers = [b.paragraph_number for b in blocks]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) >= 2
    assert len({b.line_number for b in blocks}) == len(blocks)
