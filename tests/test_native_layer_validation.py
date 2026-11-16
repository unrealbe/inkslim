"""A bad PDF text layer is a property of the document, not of one sampled page."""

from __future__ import annotations

from pathlib import Path
from textwrap import wrap
from types import SimpleNamespace

import pytest
from reportlab.pdfgen import canvas

from conversion.models import (
    JobStatus,
    PageMetrics,
    PageResult,
    QualityStatus,
    TextBlock,
    TextSource,
)
from conversion.ocr import OcrError
from conversion.pdf.analyzer import open_pdf_document
from conversion.pdf.models import PageDecision
from conversion.pdf.ocr_processor import PdfConversionJobProcessor
from conversion.pdf.processor import (
    PdfAnalysisJobProcessor,
    native_layer_is_untrusted,
    select_confirmation_pages,
    select_validation_pages,
)
from conversion.storage import JobStore
from conversion.worker import ConversionWorker

UNRELATED = "Completely different words that were never printed anywhere on this particular page."


def page_text(page_number: int) -> str:
    return (
        f"Page {page_number} of the test book describes the harbour, the old lighthouse and the "
        f"fishing boats that returned every evening during season number {page_number}."
    )


class PerPageEngine:
    """OCR stub that reads the true text of a page unless the page is listed as unreadable."""

    def __init__(self, wrong_pages=None, *, all_wrong=False, runtime_error=None):
        self.wrong_pages = set(wrong_pages or [])
        self.all_wrong = all_wrong
        self.runtime_error = runtime_error
        self.calls: list[int] = []
        self.runtime_checks = 0

    def check_runtime(self, languages):
        self.runtime_checks += 1
        if self.runtime_error is not None:
            raise self.runtime_error
        return frozenset(languages)

    def recognize(self, image_path, languages, *, page_number=1):
        assert Path(image_path).is_file()
        self.calls.append(page_number)
        wrong = self.all_wrong or page_number in self.wrong_pages
        text = UNRELATED if wrong else page_text(page_number)
        return PageResult(
            page_number=page_number,
            width=612,
            height=792,
            blocks=[
                TextBlock(
                    text=text,
                    bbox=(0.1, 0.1, 0.9, 0.2),
                    block_type="text",
                    confidence=96.0,
                    source=TextSource.TESSERACT,
                    line_number=1,
                    paragraph_number=1,
                )
            ],
            source=TextSource.TESSERACT,
            quality=QualityStatus.HIGH,
            metrics=PageMetrics(mean_ocr_confidence=96.0),
        )


def make_book(path: Path, pages: int, *, cover: bool = False) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica", 12)
    for number in range(1, pages + 1):
        text = "Cover" if cover and number == 1 else page_text(number)
        for index, line in enumerate(wrap(text, width=75)):
            pdf.drawString(60, 740 - index * 18, line)
        pdf.showPage()
    pdf.save()
    return path


def start(tmp_path: Path, engine: PerPageEngine, pages: int, *, cover: bool = False):
    source = make_book(tmp_path / "book.pdf", pages, cover=cover)
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="eng")
    worker = ConversionWorker(
        store,
        PdfConversionJobProcessor(engine=engine, native_text_sample_interval=10),
        worker_id="layer-validation",
    )
    assert worker.run_once()
    return store, job, worker


def analyses(store, job, pages: int):
    workspace = store.workspace(job.id)
    return {number: workspace.load_page_analysis(number) for number in range(1, pages + 1)}


# --- which pages are compared -------------------------------------------------------------


def test_selection_takes_first_middle_and_every_nth_eligible_page():
    assert select_validation_pages(list(range(1, 13)), 10) == [1, 7, 11]


def test_selection_counts_eligible_pages_not_page_numbers():
    # Pages 1 and 2 have too little text; the first compared page is the first eligible one.
    eligible = list(range(3, 13))
    chosen = select_validation_pages(eligible, 10)
    assert chosen[0] == 3
    assert 1 not in chosen and 2 not in chosen


@pytest.mark.parametrize("count", [1, 2, 3, 5, 9])
def test_short_documents_still_get_a_spread_of_samples(count):
    eligible = list(range(10, 10 + count))
    chosen = select_validation_pages(eligible, 10)
    assert len(chosen) >= min(3, count)
    assert chosen[0] == eligible[0]
    assert set(chosen) <= set(eligible)


def test_selection_is_empty_when_disabled_or_nothing_is_eligible():
    assert select_validation_pages([1, 2, 3], 0) == []
    assert select_validation_pages([], 10) == []


def test_interval_one_compares_every_eligible_page():
    assert select_validation_pages([4, 5, 6, 7], 1) == [4, 5, 6, 7]


def test_confirmation_pages_are_spread_and_limited():
    chosen = select_confirmation_pages(list(range(1, 51)))
    assert len(chosen) == 5
    assert chosen[0] == 1 and chosen[-1] == 50
    assert select_confirmation_pages([]) == []


# --- verdict ------------------------------------------------------------------------------


def _page(*warnings):
    return SimpleNamespace(warnings=list(warnings))


def test_verdict_needs_at_least_half_of_the_conclusive_samples_to_disagree():
    good = _page("NATIVE_OCR_SIMILARITY:1.000")
    bad = _page("NATIVE_OCR_SIMILARITY:0.010", "NATIVE_OCR_MISMATCH")
    assert not native_layer_is_untrusted([good, good, good, bad])
    assert native_layer_is_untrusted([good, good, bad, bad])
    assert native_layer_is_untrusted([bad])
    assert not native_layer_is_untrusted([good, good])
    assert not native_layer_is_untrusted([])


def test_low_ocr_confidence_is_not_evidence_against_the_text_layer():
    unsure = _page("NATIVE_OCR_SIMILARITY:0.300", "NATIVE_OCR_VALIDATION_LOW_CONFIDENCE")
    good = _page("NATIVE_OCR_SIMILARITY:1.000")
    bad = _page("NATIVE_OCR_SIMILARITY:0.010", "NATIVE_OCR_MISMATCH")
    assert not native_layer_is_untrusted([unsure, unsure, good])
    # An unsure page does not dilute a mismatch either.
    assert native_layer_is_untrusted([unsure, unsure, unsure, bad])


# --- the whole analysis -------------------------------------------------------------------


def test_good_text_layer_keeps_every_page_native_and_compares_only_a_sample(tmp_path):
    engine = PerPageEngine()
    store, job, _ = start(tmp_path, engine, 12)

    result = analyses(store, job, 12)
    assert all(item.decision is PageDecision.USE_NATIVE for item in result.values())
    assert sorted(engine.calls) == [1, 7, 11]
    workspace = store.workspace(job.id)
    for number in range(1, 13):
        stored = workspace.load_page_result(number)
        assert stored.source is TextSource.NATIVE
        assert workspace.load_page_lines(number)


def test_bad_text_layer_sends_every_unverified_page_to_ocr(tmp_path):
    engine = PerPageEngine(all_wrong=True)
    store, job, worker = start(tmp_path, engine, 12)

    result = analyses(store, job, 12)
    assert all(item.decision is PageDecision.OCR_REQUIRED for item in result.values())
    compared = sorted(engine.calls)
    assert compared == [1, 7, 11]
    workspace = store.workspace(job.id)
    for number, item in result.items():
        if number in compared:
            assert "NATIVE_OCR_MISMATCH" in item.warnings
            assert workspace.load_page_result(number).source is TextSource.TESSERACT
            # The line checkpoint of the native text is gone; the OCR stage rebuilds it.
            assert workspace.load_page_lines(number) is None
        else:
            assert "NATIVE_TEXT_LAYER_UNTRUSTED" in item.warnings
            assert item.render_dpi is not None
            # No stale native text may be left for the OCR stage to reuse.
            assert workspace.load_page_result(number) is None
            assert workspace.load_page_lines(number) is None

    # The OCR phase reads the remaining pages and reuses the three compared ones.
    assert worker.run_once()
    assert sorted(engine.calls) == list(range(1, 13))
    assert workspace.load_page_result(2).source is TextSource.TESSERACT


def test_one_isolated_mismatch_does_not_distrust_the_document(tmp_path):
    engine = PerPageEngine(wrong_pages={1})
    store, job, _ = start(tmp_path, engine, 12)

    result = analyses(store, job, 12)
    assert result[1].decision is PageDecision.OCR_REQUIRED
    assert "NATIVE_OCR_MISMATCH" in result[1].warnings
    assert all(
        item.decision is PageDecision.USE_NATIVE for number, item in result.items() if number != 1
    )
    assert not any("NATIVE_TEXT_LAYER_UNTRUSTED" in item.warnings for item in result.values())
    # A first mismatch triggers extra comparisons before the verdict.
    assert len(engine.calls) > 3


def test_cover_page_does_not_use_up_the_sample(tmp_path):
    engine = PerPageEngine(all_wrong=True)
    store, job, _ = start(tmp_path, engine, 10, cover=True)

    assert 1 not in engine.calls
    assert len(engine.calls) >= 3
    result = analyses(store, job, 10)
    assert all(item.decision is PageDecision.OCR_REQUIRED for item in result.values())


def test_missing_ocr_runtime_fails_the_job_once_instead_of_every_page(tmp_path):
    error = OcrError("OCR_ENGINE_UNAVAILABLE", "The text recognition engine could not be started.")
    engine = PerPageEngine(runtime_error=error)
    source = make_book(tmp_path / "book.pdf", 4)
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="eng")
    worker = ConversionWorker(
        store,
        PdfConversionJobProcessor(engine=engine, native_text_sample_interval=10),
        worker_id="runtime-check",
    )
    assert worker.run_once()

    failed = store.get_job(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "OCR_ENGINE_UNAVAILABLE"
    assert engine.runtime_checks == 1
    assert engine.calls == []


def test_repeating_the_validation_pass_changes_nothing(tmp_path):
    """A crash and retry runs the pass again on its own checkpoints."""
    engine = PerPageEngine(all_wrong=True)
    store, job, _ = start(tmp_path, engine, 12)
    before = {number: item.warnings[:] for number, item in analyses(store, job, 12).items()}
    calls_before = list(engine.calls)

    processor = PdfAnalysisJobProcessor(engine=engine, sample_interval=10)
    quiet_store = SimpleNamespace(
        is_cancel_requested=lambda job_id: False, update_progress=lambda *args, **kwargs: None
    )
    stored_job = store.get_job(job.id)
    with open_pdf_document(Path(stored_job.input_path)) as document:
        processor._validate_native_text_layer(
            document, stored_job, store.workspace(job.id), quiet_store, 12
        )

    assert {n: i.warnings for n, i in analyses(store, job, 12).items()} == before
    assert engine.calls == calls_before


def test_retry_after_a_crash_between_deleting_text_and_saving_the_analysis(tmp_path):
    """Delete first, save last: the analysis still says native, so the retry repairs the page."""
    engine = PerPageEngine(all_wrong=True)
    store, job, _ = start(tmp_path, engine, 12)
    workspace = store.workspace(job.id)

    # Page 2 was distrusted: simulate a crash that lost only the analysis update.
    stale = workspace.load_page_analysis(2)
    stale.decision = PageDecision.USE_NATIVE
    stale.warnings = [w for w in stale.warnings if w != "NATIVE_TEXT_LAYER_UNTRUSTED"]
    workspace.save_page_analysis(stale)
    assert workspace.load_page_result(2) is None  # the stale native text is already gone

    processor = PdfAnalysisJobProcessor(engine=engine, sample_interval=10)
    quiet_store = SimpleNamespace(
        is_cancel_requested=lambda job_id: False, update_progress=lambda *args, **kwargs: None
    )
    stored_job = store.get_job(job.id)
    with open_pdf_document(Path(stored_job.input_path)) as document:
        # The analysis loop rebuilds native text for pages still marked native, then the pass
        # judges the document again and routes the page back through OCR.
        page = document[1]
        try:
            processor._ensure_native_result(page, stale, workspace)
        finally:
            page.close()
        assert workspace.load_page_result(2).source is TextSource.NATIVE
        processor._validate_native_text_layer(document, stored_job, workspace, quiet_store, 12)

    repaired = workspace.load_page_analysis(2)
    assert repaired.decision is PageDecision.OCR_REQUIRED
    assert workspace.load_page_result(2) is None
    assert workspace.load_page_lines(2) is None


def make_scan(path: Path) -> Path:
    """One page that is only an image, so the analysis sends it to OCR."""
    import io

    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    image = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(image).text((50, 50), "scanned page", fill=0)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.drawImage(ImageReader(buffer), 0, 0, width=612, height=792)
    pdf.showPage()
    pdf.save()
    return path


def test_missing_ocr_runtime_fails_a_scan_only_job_once_before_any_page(tmp_path):
    error = OcrError("OCR_LANGUAGE_UNAVAILABLE", "A required OCR language is not installed.")
    engine = PerPageEngine(runtime_error=error)
    source = make_scan(tmp_path / "scan.pdf")
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    job = store.create_job(source, source.name, language="eng")
    worker = ConversionWorker(
        store,
        PdfConversionJobProcessor(engine=engine, native_text_sample_interval=10),
        worker_id="scan-runtime-check",
    )
    assert worker.run_once()  # analysis: no native page, so nothing is compared
    assert store.workspace(job.id).load_page_analysis(1).decision is PageDecision.OCR_REQUIRED
    assert worker.run_once()  # OCR phase

    failed = store.get_job(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "OCR_LANGUAGE_UNAVAILABLE"
    assert engine.runtime_checks == 1
    assert engine.calls == []
