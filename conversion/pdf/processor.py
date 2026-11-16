"""Worker processor for PDF analysis, including sampled native-text validation."""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

import pypdfium2 as pdfium

from ..models import Job, JobStatus, TextSource
from ..language import ocr_languages_for_selection
from ..ocr import OcrEngine, OcrError, TesseractOcrEngine
from ..storage import JobStore, JobWorkspace
from ..lines import reconstruct_lines
from ..worker import JobCancelled, JobExecutionResult, JobProcessingError
from .analyzer import PdfAnalysisError, analyze_pdf_page, open_pdf_document
from .models import PageDecision
from .native_text import (
    NativeTextGeometryError,
    extract_native_page_result,
    extract_native_text,
)
from .renderer import PDFRenderLimits, RenderPlanError, build_render_plan, render_pdf_page
from .images import extract_page_images

logger = logging.getLogger(__name__)
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NATIVE_VALIDATION_MIN_CHARS = 40
_NATIVE_VALIDATION_SIMILARITY = 0.72
# Minimum number of pages compared against OCR, however short the document is.
_NATIVE_VALIDATION_MIN_SAMPLES = 3
# After a first mismatch, up to this many extra pages are compared before deciding
# whether the whole text layer is untrusted.
_NATIVE_VALIDATION_CONFIRMATION_SAMPLES = 5
# The text layer is untrusted when at least this share of the conclusive samples disagrees.
_NATIVE_VALIDATION_UNTRUSTED_SHARE = 0.5

_WARNING_SIMILARITY_PREFIX = "NATIVE_OCR_SIMILARITY:"
_WARNING_MISMATCH = "NATIVE_OCR_MISMATCH"
_WARNING_LOW_CONFIDENCE = "NATIVE_OCR_VALIDATION_LOW_CONFIDENCE"
_WARNING_UNTRUSTED = "NATIVE_TEXT_LAYER_UNTRUSTED"


def _normalized_tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text)]


def native_ocr_similarity(native_text: str, ocr_text: str) -> float:
    """Return a language-neutral token similarity score in the range 0..1."""
    native_tokens = _normalized_tokens(native_text)
    ocr_tokens = _normalized_tokens(ocr_text)
    if not native_tokens or not ocr_tokens:
        return 0.0
    return SequenceMatcher(a=native_tokens, b=ocr_tokens).ratio()


def _evenly_spaced(items: list[int], count: int) -> list[int]:
    """Pick ``count`` items spread over the list, always including the first one."""
    if count <= 0 or not items:
        return []
    count = min(count, len(items))
    if count == 1:
        return [items[0]]
    positions = {round(i * (len(items) - 1) / (count - 1)) for i in range(count)}
    return [items[position] for position in sorted(positions)]


def select_validation_pages(eligible: list[int], interval: int) -> list[int]:
    """Choose the pages whose native text is compared with OCR.

    ``eligible`` holds the page numbers that have enough native text, in document order.
    Counting eligible pages (not page numbers) means a cover page or an image page can
    never use up the sample. The first eligible page, the middle one and every
    ``interval``-th one are taken, and short documents still get at least
    ``_NATIVE_VALIDATION_MIN_SAMPLES`` pages spread over the text.
    """
    if interval <= 0 or not eligible:
        return []
    count = len(eligible)
    positions = set(range(0, count, interval))
    positions.add(count // 2)
    chosen = {eligible[position] for position in positions}
    if len(chosen) < min(_NATIVE_VALIDATION_MIN_SAMPLES, count):
        chosen.update(_evenly_spaced(eligible, _NATIVE_VALIDATION_MIN_SAMPLES))
    return sorted(chosen)


def select_confirmation_pages(candidates: list[int]) -> list[int]:
    """Pick extra pages to compare after a first mismatch (candidates are not yet compared)."""
    return _evenly_spaced(candidates, _NATIVE_VALIDATION_CONFIRMATION_SAMPLES)


def _was_native_candidate(analysis) -> bool:
    """True for pages that were native text when analyzed, even if validation later moved them."""
    if analysis.decision is PageDecision.USE_NATIVE:
        return True
    return any(
        warning.startswith("NATIVE_OCR_") or warning == _WARNING_UNTRUSTED
        for warning in analysis.warnings
    )


def _validation_eligible(analysis) -> bool:
    return (
        _was_native_candidate(analysis)
        and analysis.meaningful_character_count >= _NATIVE_VALIDATION_MIN_CHARS
    )


def _is_compared(analysis) -> bool:
    """True once a page has been through validation (accepted, mismatched or skipped)."""
    return any(
        warning.startswith(_WARNING_SIMILARITY_PREFIX)
        or warning.startswith("NATIVE_OCR_VALIDATION_")
        or warning == _WARNING_MISMATCH
        for warning in analysis.warnings
    )


def _is_verified_good(analysis) -> bool:
    warnings = analysis.warnings
    return (
        any(warning.startswith(_WARNING_SIMILARITY_PREFIX) for warning in warnings)
        and _WARNING_MISMATCH not in warnings
        and _WARNING_LOW_CONFIDENCE not in warnings
    )


def native_layer_is_untrusted(analyses) -> bool:
    """Decide from the compared pages whether the document's text layer cannot be trusted.

    Only conclusive comparisons count: a page that OCR itself read with low confidence
    says nothing about the text layer.
    """
    mismatched = sum(1 for analysis in analyses if _WARNING_MISMATCH in analysis.warnings)
    verified = sum(1 for analysis in analyses if _is_verified_good(analysis))
    conclusive = mismatched + verified
    return mismatched > 0 and mismatched >= _NATIVE_VALIDATION_UNTRUSTED_SHARE * conclusive


class PdfAnalysisJobProcessor:
    """Analyze pages and validate a deterministic sample of apparently good native text."""

    def __init__(
        self,
        render_limits: PDFRenderLimits | None = None,
        *,
        engine: OcrEngine | None = None,
        sample_interval: int = 0,
        timeout_seconds: int = 120,
    ):
        if sample_interval < 0:
            raise ValueError("sample_interval cannot be negative")
        self.render_limits = render_limits or PDFRenderLimits()
        self.engine = engine or TesseractOcrEngine(timeout_seconds=timeout_seconds)
        self.sample_interval = sample_interval
        self._runtime_checked = False

    def _validate_native_sample(self, page, analysis, job, workspace) -> None:
        # The caller decides which pages are compared; see select_validation_pages().
        if analysis.decision is not PageDecision.USE_NATIVE:
            return

        native = extract_native_text(page).text
        if len(native.strip()) < _NATIVE_VALIDATION_MIN_CHARS:
            analysis.warnings.append("NATIVE_OCR_VALIDATION_SKIPPED_SHORT_TEXT")
            return

        try:
            plan = build_render_plan(
                analysis.width, analysis.height,
                limits=self.render_limits,
            )
        except RenderPlanError:
            analysis.decision = PageDecision.OCR_REQUIRED
            analysis.warnings.append("NATIVE_OCR_VALIDATION_RENDER_LIMIT_EXCEEDED")
            return

        analysis.render_dpi = plan.actual_dpi
        analysis.render_width_px = plan.width_px
        analysis.render_height_px = plan.height_px
        destination = workspace.rendered_page_path(analysis.page_number)
        page_limits = PDFRenderLimits(
            dpi=plan.actual_dpi,
            minimum_dpi=plan.actual_dpi,
            max_pixels=self.render_limits.max_pixels,
            max_dimension=self.render_limits.max_dimension,
        )
        render_pdf_page(page, destination, limits=page_limits)

        # A PageResult written before the analysis checkpoint makes a crash between
        # the two writes idempotent: a retry compares against the same OCR output.
        result = workspace.load_page_result(analysis.page_number)
        if result is None or result.source is not TextSource.TESSERACT:
            result = self.engine.recognize(
                destination, ocr_languages_for_selection(job.language), page_number=analysis.page_number
            )
            workspace.save_page_result(result)

        ocr_text = "\n".join(block.text for block in result.blocks)
        similarity = native_ocr_similarity(native, ocr_text)
        analysis.warnings.append(f"NATIVE_OCR_SIMILARITY:{similarity:.3f}")
        if result.quality.value != "high":
            analysis.decision = PageDecision.OCR_REQUIRED
            analysis.warnings.append("NATIVE_OCR_VALIDATION_LOW_CONFIDENCE")
        elif similarity < _NATIVE_VALIDATION_SIMILARITY:
            analysis.decision = PageDecision.OCR_REQUIRED
            analysis.warnings.append("NATIVE_OCR_MISMATCH")

    def _prepare_ocr_render(self, analysis) -> None:
        if analysis.render_dpi is not None:
            return
        try:
            plan = build_render_plan(
                analysis.width, analysis.height, limits=self.render_limits
            )
            analysis.render_dpi = plan.actual_dpi
            analysis.render_width_px = plan.width_px
            analysis.render_height_px = plan.height_px
            analysis.warnings.extend(plan.warnings)
        except RenderPlanError:
            analysis.warnings.append("RENDER_LIMIT_EXCEEDED")

    def _ensure_native_result(self, page, analysis, workspace) -> None:
        existing = workspace.load_page_result(analysis.page_number)
        if existing is not None and existing.source is TextSource.NATIVE:
            result = existing
        else:
            try:
                result = extract_native_page_result(
                    page,
                    analysis.page_number,
                    native_text_score=analysis.native_text_quality,
                )
            except NativeTextGeometryError:
                analysis.decision = PageDecision.OCR_REQUIRED
                analysis.warnings.append("NATIVE_TEXT_EXTRACTION_FAILED")
                self._prepare_ocr_render(analysis)
                return
            workspace.save_page_result(result)

        # Native and OCR pages share the same downstream line checkpoint format.
        workspace.save_page_lines(
            analysis.page_number,
            [line.to_dict() for line in reconstruct_lines(result)],
        )

    def _ensure_page_images(self, page, page_number, workspace):
        existing = workspace.load_page_images(page_number)
        if existing is not None:
            return existing
        extracted = extract_page_images(page, page_number, workspace)
        workspace.save_page_images(extracted)
        return extracted

    def _discard_native_text(self, page_number: int, workspace: JobWorkspace) -> None:
        """Remove native text checkpoints of a page that is now routed through OCR.

        The OCR stage reuses any existing page result and line checkpoint, so stale native
        text must not survive. A result written by OCR validation is kept.
        """
        workspace.page_lines_path(page_number).unlink(missing_ok=True)
        existing = workspace.load_page_result(page_number)
        if existing is not None and existing.source is TextSource.NATIVE:
            workspace.page_result_path(page_number).unlink(missing_ok=True)

    def _check_ocr_runtime(self, job: Job) -> None:
        checker = getattr(self.engine, "check_runtime", None)
        if callable(checker):
            checker(ocr_languages_for_selection(job.language))

    def _compare_pages(self, document, page_numbers, analyses, job, workspace, store) -> None:
        """Compare native text with OCR for the given pages and checkpoint every outcome."""
        total_pages = len(analyses)
        for page_number in page_numbers:
            analysis = analyses[page_number]
            if analysis.decision is not PageDecision.USE_NATIVE or _is_compared(analysis):
                continue  # already compared on an earlier run, or already routed to OCR
            if store.is_cancel_requested(job.id):
                raise JobCancelled(f"Cancellation requested before checking page {page_number}")
            if not self._runtime_checked:
                # A missing engine or language is a problem of the job, not of one page.
                self._check_ocr_runtime(job)
                self._runtime_checked = True
            store.update_progress(
                job.id,
                status=JobStatus.ANALYZING,
                current_page=page_number,
                total_pages=total_pages,
                completed_pages=total_pages,
                message=f"Checking native text of page {page_number} against OCR",
            )
            page = document[page_number - 1]
            try:
                try:
                    self._validate_native_sample(page, analysis, job, workspace)
                except OcrError as exc:
                    if exc.job_level:
                        raise
                    # Validation failure must not silently bless native text.
                    analysis.decision = PageDecision.OCR_REQUIRED
                    analysis.warnings.append(f"NATIVE_OCR_VALIDATION_FAILED:{exc.code}")
                    self._prepare_ocr_render(analysis)
                if analysis.decision is PageDecision.USE_NATIVE:
                    # Validation overwrote the page result with the OCR text; restore native.
                    self._ensure_native_result(page, analysis, workspace)
            finally:
                page.close()
            if analysis.decision is not PageDecision.USE_NATIVE:
                self._discard_native_text(page_number, workspace)
            workspace.save_page_analysis(analysis)

    def _distrust_native_layer(self, analyses, workspace) -> None:
        """Route every native page that was not verified through OCR."""
        for analysis in analyses.values():
            if analysis.decision is not PageDecision.USE_NATIVE or _is_verified_good(analysis):
                continue
            # Delete first, save the analysis last: a crash in between is repaired on retry.
            self._discard_native_text(analysis.page_number, workspace)
            analysis.decision = PageDecision.OCR_REQUIRED
            analysis.warnings.append(_WARNING_UNTRUSTED)
            self._prepare_ocr_render(analysis)
            workspace.save_page_analysis(analysis)

    def _validate_native_text_layer(self, document, job, workspace, store, total_pages) -> None:
        """Second analysis pass: compare a sample with OCR and judge the whole text layer.

        A bad text layer is a property of the document (for example an old OCR run under a
        scan), so one disagreeing sample page makes more pages be compared, and when most of
        the compared pages disagree no unverified native page is trusted.
        """
        if self.sample_interval == 0:
            return
        analyses = {
            number: workspace.load_page_analysis(number) for number in range(1, total_pages + 1)
        }
        if any(analysis is None for analysis in analyses.values()):
            raise JobProcessingError(
                "ANALYSIS_CHECKPOINT_MISSING",
                "PDF analysis data is missing; the job must be analyzed again.",
            )
        eligible = [number for number, item in analyses.items() if _validation_eligible(item)]
        first_round = select_validation_pages(eligible, self.sample_interval)
        self._compare_pages(document, first_round, analyses, job, workspace, store)

        def mismatched() -> int:
            return sum(1 for item in analyses.values() if _WARNING_MISMATCH in item.warnings)

        def verified() -> int:
            return sum(1 for item in analyses.values() if _is_verified_good(item))

        if 0 < mismatched() < mismatched() + verified():
            chosen = set(first_round)
            remaining = [number for number in eligible if number not in chosen]
            self._compare_pages(
                document, select_confirmation_pages(remaining), analyses, job, workspace, store
            )
        if native_layer_is_untrusted(analyses.values()):
            self._distrust_native_layer(analyses, workspace)

    def __call__(
        self,
        job: Job,
        workspace: JobWorkspace,
        store: JobStore,
    ) -> JobExecutionResult:
        try:
            document = open_pdf_document(Path(job.input_path))
            with document:
                total_pages = len(document)
                if total_pages < 1:
                    raise PdfAnalysisError("EMPTY_PDF", "The PDF does not contain any pages")

                completed = workspace.completed_analysis_count(total_pages)
                next_page = workspace.first_missing_analysis(total_pages)
                store.reconcile_analysis_progress(
                    job.id,
                    total_pages=total_pages,
                    completed_pages=completed,
                    next_page=next_page if next_page <= total_pages else None,
                )

                for page_index in range(total_pages):
                    page_number = page_index + 1
                    if store.is_cancel_requested(job.id):
                        raise JobCancelled(
                            f"Cancellation requested before PDF page {page_number}"
                        )

                    cached_analysis = workspace.load_page_analysis(page_number)
                    if cached_analysis is not None:
                        page = document[page_index]
                        try:
                            image_checkpoint = self._ensure_page_images(page, page_number, workspace)
                            cached_analysis.warnings.extend(
                                warning for warning in image_checkpoint.warnings
                                if warning not in cached_analysis.warnings
                            )
                            if cached_analysis.decision is PageDecision.USE_NATIVE:
                                self._ensure_native_result(page, cached_analysis, workspace)
                        finally:
                            page.close()
                        workspace.save_page_analysis(cached_analysis)
                        continue

                    page = document[page_index]
                    try:
                        analysis = analyze_pdf_page(
                            page, page_number, render_limits=self.render_limits
                        )
                        if analysis.decision is PageDecision.USE_NATIVE:
                            self._ensure_native_result(page, analysis, workspace)
                        image_checkpoint = self._ensure_page_images(page, page_number, workspace)
                        analysis.warnings.extend(
                            warning for warning in image_checkpoint.warnings
                            if warning not in analysis.warnings
                        )
                    finally:
                        page.close()

                    # The result checkpoint is durable before the analysis checkpoint.
                    # A retry can safely recreate a native result or reuse sampled OCR.
                    workspace.save_page_analysis(analysis)
                    completed += 1
                    store.update_progress(
                        job.id,
                        status=JobStatus.ANALYZING,
                        current_page=page_number,
                        total_pages=total_pages,
                        completed_pages=completed,
                        message=(
                            f"Analyzed page {page_number} of {total_pages} "
                            f"({analysis.decision.value})"
                        ),
                    )

                self._runtime_checked = False
                self._validate_native_text_layer(document, job, workspace, store, total_pages)

                if store.is_cancel_requested(job.id):
                    raise JobCancelled("Cancellation requested after PDF analysis")

            return JobExecutionResult(next_status=JobStatus.READY_FOR_OCR)
        except JobCancelled:
            raise
        except PdfAnalysisError as exc:
            raise JobProcessingError(exc.code, exc.public_message) from exc
        except pdfium.PdfiumError as exc:
            logger.exception("PDFium failed while analyzing conversion job %s", job.id)
            raise JobProcessingError(
                "PDF_ANALYSIS_FAILED", "The PDF could not be analyzed safely."
            ) from exc
        except OcrError as exc:
            raise JobProcessingError(exc.code, exc.public_message) from exc
        except OSError as exc:
            logger.exception("Storage failure while analyzing conversion job %s", job.id)
            raise JobProcessingError(
                "STORAGE_ERROR", "Inkslim could not save the PDF analysis results."
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected PDF analysis failure for job %s", job.id)
            raise JobProcessingError(
                "PDF_ANALYSIS_FAILED", "The PDF could not be analyzed safely."
            ) from exc
