"""Single-process conversion worker with restart recovery and cooperative cancel."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import Job, JobStatus
from .storage import JobStore, JobWorkspace

logger = logging.getLogger(__name__)


class JobCancelled(Exception):
    """Raised by a processor when cancellation is observed between pages."""


class JobProcessingError(Exception):
    """A safe, user-facing processing failure with a stable error code."""

    def __init__(self, error_code: str, public_message: str):
        super().__init__(public_message)
        self.error_code = error_code
        self.public_message = public_message


@dataclass(slots=True)
class JobExecutionResult:
    output_path: Path | None = None
    report_path: Path | None = None
    next_status: JobStatus = JobStatus.COMPLETED


class JobProcessor(Protocol):
    def __call__(
        self, job: Job, workspace: JobWorkspace, store: JobStore
    ) -> JobExecutionResult | None: ...


class ConversionWorker:
    """Claims and runs one job at a time using an injected conversion processor."""

    def __init__(
        self,
        store: JobStore,
        processor: JobProcessor,
        *,
        worker_id: str | None = None,
        poll_seconds: float = 2.0,
    ):
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.store = store
        self.processor = processor
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_seconds = poll_seconds

    def run_once(self) -> bool:
        """Run one queued job; return False when the queue is empty."""
        job = self.store.claim_next_job(self.worker_id)
        if job is None:
            return False

        try:
            if self.store.is_cancel_requested(job.id):
                self.store.mark_cancelled(job.id)
                return True

            result = self.processor(job, self.store.workspace(job.id), self.store)
            if self.store.is_cancel_requested(job.id):
                self.store.mark_cancelled(job.id)
            else:
                result = result or JobExecutionResult()
                if result.next_status is JobStatus.READY_FOR_OCR:
                    self.store.mark_ready_for_ocr(job.id)
                elif result.next_status is JobStatus.EXTRACTING:
                    self.store.mark_extracting(job.id, report_path=result.report_path)
                elif result.next_status is JobStatus.COMPLETED:
                    self.store.complete_job(
                        job.id,
                        output_path=result.output_path,
                        report_path=result.report_path,
                    )
                else:
                    raise ValueError("Worker processor returned an unsupported next status")
        except JobCancelled:
            self.store.mark_cancelled(job.id)
        except JobProcessingError as exc:
            logger.warning(
                "Conversion job %s failed (%s): %s", job.id, exc.error_code, exc
            )
            if self.store.is_cancel_requested(job.id):
                self.store.mark_cancelled(job.id)
            else:
                self.store.fail_job(job.id, exc.error_code, exc.public_message)
        except Exception:
            logger.exception("Conversion job %s failed", job.id)
            if self.store.is_cancel_requested(job.id):
                self.store.mark_cancelled(job.id)
            else:
                self.store.fail_job(
                    job.id,
                    "CONVERSION_FAILED",
                    "The conversion failed. Check the worker logs for details.",
                )
        return True

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """Recover interrupted jobs once, then poll until the stop event is set."""
        stop_event = stop_event or threading.Event()
        recovered = self.store.recover_interrupted_jobs()
        try:
            self.store.cleanup_expired_jobs()
        except Exception:
            logger.exception("Could not clean up expired conversion jobs during startup")
        if recovered:
            logger.info("Requeued %d interrupted conversion job(s)", recovered)

        last_cleanup = time.monotonic()
        while not stop_event.is_set():
            if time.monotonic() - last_cleanup >= 60:
                try:
                    self.store.cleanup_expired_jobs()
                except Exception:
                    logger.exception("Could not clean up expired conversion jobs")
                last_cleanup = time.monotonic()
            if not self.run_once():
                stop_event.wait(self.poll_seconds)
