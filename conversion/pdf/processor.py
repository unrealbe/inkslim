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

logger = logging.getLogger(__name__)
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NATIVE_VALIDATION_MIN_CHARS = 40
_NATIVE_VALIDATION_SIMILARITY = 0.72


def _normalized_tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text)]


def native_ocr_similarity(native_text: str, ocr_text: str) -> float:
    """Return a language-neutral token similarity score in the range 0..1."""
    native_tokens = _normalized_tokens(native_text)
    ocr_tokens = _normalized_tokens(ocr_text)
    if not native_tokens or not ocr_tokens:
        return 0.0
    return SequenceMatcher(a=native_tokens, b=ocr_tokens).ratio()


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

    def _validate_native_sample(self, page, analysis, job, workspace) -> None:
        if self.sample_interval == 0 or analysis.decision is not PageDecision.USE_NATIVE:
            return
        # Pages 1, 1+N, 1+2N... are stable across retries and deployments.
        if (analysis.page_number - 1) % self.sample_interval != 0:
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
                        if cached_analysis.decision is PageDecision.USE_NATIVE:
                            page = document[page_index]
                            try:
                                self._ensure_native_result(
                                    page, cached_analysis, workspace
                                )
                            finally:
                                page.close()
                            if cached_analysis.decision is not PageDecision.USE_NATIVE:
                                workspace.save_page_analysis(cached_analysis)
                        continue

                    page = document[page_index]
                    try:
                        analysis = analyze_pdf_page(
                            page, page_number, render_limits=self.render_limits
                        )
                        if analysis.decision is PageDecision.USE_NATIVE:
                            try:
                                self._validate_native_sample(page, analysis, job, workspace)
                            except OcrError as exc:
                                # Validation failure must not silently bless native text.
                                # Route the page through the regular OCR stage instead.
                                analysis.decision = PageDecision.OCR_REQUIRED
                                analysis.warnings.append(
                                    f"NATIVE_OCR_VALIDATION_FAILED:{exc.code}"
                                )
                                self._prepare_ocr_render(analysis)

                        if analysis.decision is PageDecision.USE_NATIVE:
                            self._ensure_native_result(page, analysis, workspace)
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
