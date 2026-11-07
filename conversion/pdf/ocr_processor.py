"""Worker processor for the PDF OCR phase and phase dispatch."""

from __future__ import annotations

import logging
from pathlib import Path

import pypdfium2 as pdfium

from ..models import Job, JobStatus
from ..ocr import OcrEngine, OcrError, TesseractOcrEngine
from ..storage import JobStore, JobWorkspace
from ..worker import JobCancelled, JobExecutionResult, JobProcessingError
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
                    if workspace.load_page_result(page_number) is not None:
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
                    store.update_progress(
                        job.id,
                        status=JobStatus.OCR,
                        current_page=page_number,
                        total_pages=total_pages,
                        message=f"OCR processed page {page_number} of {total_pages}",
                    )
                if store.is_cancel_requested(job.id):
                    raise JobCancelled("Cancellation requested after OCR")
            return JobExecutionResult(next_status=JobStatus.EXTRACTING)
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

    def __init__(self, render_limits=None, *, engine: OcrEngine | None = None, timeout_seconds: int = 120):
        from .processor import PdfAnalysisJobProcessor

        self.analysis = PdfAnalysisJobProcessor(render_limits=render_limits)
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
