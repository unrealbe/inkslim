"""Worker processor for the PDF OCR phase and phase dispatch."""

from __future__ import annotations

import logging
from pathlib import Path

import pypdfium2 as pdfium

from ..models import Job, JobStatus
from ..ocr import OcrEngine, OcrError, TesseractOcrEngine
from ..storage import JobStore, JobWorkspace
from ..worker import JobCancelled, JobExecutionResult, JobProcessingError
from ..quality_report import build_quality_report
from ..lines import ReconstructedLine, reconstruct_lines
from ..paragraphs import reconstruct_paragraphs
from ..dehyphenation import dehyphenate_paragraphs
from .analyzer import PdfAnalysisError, open_pdf_document
from .models import PageDecision
from .renderer import PDFRenderLimits, RenderPlanError, render_pdf_page

logger = logging.getLogger(__name__)


class PdfOcrJobProcessor:
    """OCR only pages explicitly classified OCR_REQUIRED; persist each result."""

    def __init__(self, render_limits=None, *, engine: OcrEngine | None = None, timeout_seconds: int = 120):
        self.render_limits = render_limits or PDFRenderLimits()
        self.engine = engine or TesseractOcrEngine(timeout_seconds=timeout_seconds)

    def __call__(self, job: Job, workspace: JobWorkspace, store: JobStore) -> JobExecutionResult:
        try:
            document = open_pdf_document(Path(job.input_path))
            with document:
                total_pages = len(document)
                if total_pages < 1:
                    raise PdfAnalysisError("EMPTY_PDF", "The PDF does not contain any pages")
                for page_index in range(total_pages):
                    page_number = page_index + 1
                    if store.is_cancel_requested(job.id):
                        raise JobCancelled(f"Cancellation requested before OCR page {page_number}")
                    analysis = workspace.load_page_analysis(page_number)
                    if analysis is None:
                        raise JobProcessingError(
                            "ANALYSIS_CHECKPOINT_MISSING",
                            "PDF analysis data is missing; the job must be analyzed again.",
                        )
                    if analysis.decision is not PageDecision.OCR_REQUIRED:
                        continue
                    existing_result = workspace.load_page_result(page_number)
                    if existing_result is not None:
                        if workspace.load_page_lines(page_number) is None:
                            workspace.save_page_lines(
                                page_number,
                                [line.to_dict() for line in reconstruct_lines(existing_result)],
                            )
                        continue
                    if analysis.render_dpi is None:
                        raise JobProcessingError(
                            "OCR_RENDER_UNAVAILABLE",
                            f"Page {page_number} cannot be rendered within the configured safety limits.",
                        )

                    page = document[page_index]
                    try:
                        destination = workspace.rendered_page_path(page_number)
                        page_limits = PDFRenderLimits(
                            dpi=analysis.render_dpi,
                            minimum_dpi=analysis.render_dpi,
                            max_pixels=self.render_limits.max_pixels,
                            max_dimension=self.render_limits.max_dimension,
                        )
                        render_pdf_page(page, destination, limits=page_limits)
                    except RenderPlanError as exc:
                        raise JobProcessingError(
                            "OCR_RENDER_FAILED",
                            f"Page {page_number} could not be rendered safely for text recognition.",
                        ) from exc
                    finally:
                        page.close()

                    try:
                        result = self.engine.recognize(
                            destination, job.language.split("+"), page_number=page_number
                        )
                    except OcrError as exc:
                        raise JobProcessingError(exc.code, exc.public_message) from exc

                    workspace.save_page_result(result)
                    workspace.save_page_lines(
                        page_number,
                        [line.to_dict() for line in reconstruct_lines(result)],
                    )
                    store.update_progress(
                        job.id,
                        status=JobStatus.OCR,
                        current_page=page_number,
                        total_pages=total_pages,
                        message=f"OCR processed page {page_number} of {total_pages}",
                    )
                # Paragraphs are the first shared post-processing representation for
                # native and OCR text. EMPTY pages receive an explicit empty checkpoint.
                for page_number in range(1, total_pages + 1):
                    if store.is_cancel_requested(job.id):
                        raise JobCancelled(
                            f"Cancellation requested before paragraph page {page_number}"
                        )
                    analysis = workspace.load_page_analysis(page_number)
                    if analysis is None:
                        raise JobProcessingError(
                            "ANALYSIS_CHECKPOINT_MISSING",
                            "PDF analysis data is missing; the job must be analyzed again.",
                        )
                    if analysis.decision.value == "empty":
                        lines = []
                    else:
                        raw_lines = workspace.load_page_lines(page_number)
                        if raw_lines is None:
                            page_result = workspace.load_page_result(page_number)
                            if page_result is None:
                                raise JobProcessingError(
                                    "TEXT_CHECKPOINT_MISSING",
                                    f"Text data for page {page_number} is missing.",
                                )
                            reconstructed = reconstruct_lines(page_result)
                            workspace.save_page_lines(
                                page_number,
                                [line.to_dict() for line in reconstructed],
                            )
                            raw_lines = [line.to_dict() for line in reconstructed]
                        try:
                            lines = [ReconstructedLine(
                                line_index=int(item["line_index"]),
                                text=str(item["text"]),
                                bbox=tuple(item["bbox"]),
                                confidence=item.get("confidence"),
                                source=str(item["source"]),
                                source_line_number=item.get("source_line_number"),
                                source_paragraph_number=item.get("source_paragraph_number"),
                            ) for item in raw_lines]
                        except (KeyError, TypeError, ValueError) as exc:
                            raise JobProcessingError(
                                "LINE_CHECKPOINT_INVALID",
                                f"Text line data for page {page_number} is invalid.",
                            ) from exc
                    raw_paragraphs = [
                        paragraph.to_dict() for paragraph in reconstruct_paragraphs(lines)
                    ]
                    workspace.save_page_paragraphs(page_number, raw_paragraphs)

                    # Keep the original paragraph checkpoint immutable. The
                    # dehyphenated representation is a separate resumable stage.
                    if workspace.load_page_dehyphenated(page_number) is None:
                        processed, removed_soft_hyphens = dehyphenate_paragraphs(
                            raw_paragraphs
                        )
                        workspace.save_page_dehyphenated(
                            page_number,
                            processed,
                            removed_soft_hyphens=removed_soft_hyphens,
                        )
                    store.update_progress(
                        job.id,
                        status=JobStatus.OCR,
                        current_page=page_number,
                        total_pages=total_pages,
                        message=f"Reconstructed paragraphs on page {page_number} of {total_pages}",
                    )
                if store.is_cancel_requested(job.id):
                    raise JobCancelled("Cancellation requested after paragraph reconstruction")
            report_path = build_quality_report(job, workspace, total_pages=total_pages)
            return JobExecutionResult(
                report_path=report_path,
                next_status=JobStatus.EXTRACTING,
            )
        except (JobCancelled, JobProcessingError):
            raise
        except PdfAnalysisError as exc:
            raise JobProcessingError(exc.code, exc.public_message) from exc
        except pdfium.PdfiumError as exc:
            logger.exception("PDFium failed during OCR for conversion job %s", job.id)
            raise JobProcessingError(
                "OCR_RENDER_FAILED",
                "A PDF page could not be rendered safely for text recognition.",
            ) from exc
        except OSError as exc:
            logger.exception("Storage failure during OCR for conversion job %s", job.id)
            raise JobProcessingError(
                "STORAGE_ERROR", "Inkslim could not save the OCR results."
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected OCR failure for conversion job %s", job.id)
            raise JobProcessingError(
                "OCR_FAILED", "Text recognition failed. Check the worker logs for details."
            ) from exc


class PdfConversionJobProcessor:
    """Dispatch a claimed job to its current PDF pipeline phase."""

    def __init__(
        self, render_limits=None, *, engine: OcrEngine | None = None,
        timeout_seconds: int = 120, native_text_sample_interval: int = 10,
    ):
        from .processor import PdfAnalysisJobProcessor

        self.analysis = PdfAnalysisJobProcessor(
            render_limits=render_limits,
            engine=engine,
            sample_interval=native_text_sample_interval,
            timeout_seconds=timeout_seconds,
        )
        self.ocr = PdfOcrJobProcessor(
            render_limits=render_limits, engine=engine, timeout_seconds=timeout_seconds
        )

    def __call__(self, job: Job, workspace: JobWorkspace, store: JobStore) -> JobExecutionResult:
        if job.status is JobStatus.ANALYZING:
            return self.analysis(job, workspace, store)
        if job.status is JobStatus.OCR:
            return self.ocr(job, workspace, store)
        raise JobProcessingError(
            "UNSUPPORTED_JOB_PHASE",
            "The conversion job is in an unsupported processing phase.",
        )
