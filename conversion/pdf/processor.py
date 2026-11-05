"""Worker processor for the PDF analysis phase (no OCR or EPUB generation yet)."""

from __future__ import annotations

import logging
from pathlib import Path

import pypdfium2 as pdfium

from ..models import Job, JobStatus
from ..storage import JobStore, JobWorkspace
from ..worker import JobCancelled, JobExecutionResult, JobProcessingError
from .analyzer import PdfAnalysisError, analyze_pdf_page, open_pdf_document
from .renderer import PDFRenderLimits

logger = logging.getLogger(__name__)


class PdfAnalysisJobProcessor:
    """Analyze a PDF sequentially and persist one durable checkpoint per page."""

    def __init__(self, render_limits: PDFRenderLimits | None = None):
        self.render_limits = render_limits or PDFRenderLimits()

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
                    raise PdfAnalysisError(
                        "EMPTY_PDF", "The PDF does not contain any pages"
                    )

                # Disk checkpoints are the source of truth. Reconcile the SQLite
                # dashboard before processing, including a crash after checkpoint
                # fsync/rename but before the previous progress UPDATE committed.
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

                    cached = workspace.load_page_analysis(page_number)
                    if cached is not None:
                        continue

                    page = document[page_index]
                    try:
                        analysis = analyze_pdf_page(
                            page,
                            page_number,
                            render_limits=self.render_limits,
                        )
                    finally:
                        page.close()

                    # Checkpoint first. SQLite progress follows, and can always be
                    # repaired from disk if the worker stops between these writes.
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
                "PDF_ANALYSIS_FAILED",
                "The PDF could not be analyzed safely.",
            ) from exc
        except OSError as exc:
            logger.exception("Storage failure while analyzing conversion job %s", job.id)
            raise JobProcessingError(
                "STORAGE_ERROR",
                "Inkslim could not save the PDF analysis results.",
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected PDF analysis failure for job %s", job.id)
            raise JobProcessingError(
                "PDF_ANALYSIS_FAILED",
                "The PDF could not be analyzed safely.",
            ) from exc
