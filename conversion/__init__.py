"""Local, resumable document-conversion infrastructure for Inkslim."""

from .document import (
    DOCUMENT_MODEL_VERSION,
    DocumentChapter,
    DocumentModel,
    DocumentParagraph,
    build_document_model,
)
from .models import (
    Job,
    JobStatus,
    PageMetrics,
    PageResult,
    QualityStatus,
    TextBlock,
    TextSource,
)
from .storage import JobNotFoundError, JobStore, JobWorkspace
from .worker import (
    ConversionWorker,
    JobCancelled,
    JobExecutionResult,
    JobProcessingError,
)

__all__ = [
    "ConversionWorker",
    "DOCUMENT_MODEL_VERSION",
    "DocumentChapter",
    "DocumentModel",
    "DocumentParagraph",
    "Job",
    "JobCancelled",
    "JobExecutionResult",
    "JobNotFoundError",
    "JobStatus",
    "JobStore",
    "JobWorkspace",
    "PageMetrics",
    "PageResult",
    "QualityStatus",
    "TextBlock",
    "TextSource",
    "build_document_model",
]
