"""Public HTTP API for asynchronous PDF-to-EPUB conversion jobs."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from uuid import UUID

from flask import Blueprint, current_app, jsonify, request, send_file, url_for

from .config import ConversionConfig
from .epub.validation import validate_epub
from .language import normalize_language_selection
from .models import Job, JobStatus
from .pdf.analyzer import PdfAnalysisError, open_pdf_document
from .storage import InvalidJobStateError, JobNotFoundError, JobStore

logger = logging.getLogger(__name__)
conversion_api = Blueprint("conversion_api", __name__, url_prefix="/api/conversions")


def _store() -> JobStore:
    store = current_app.extensions.get("conversion_store")
    if store is None:
        config = ConversionConfig.from_env()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        store = JobStore(config.database_path, config.jobs_dir)
        current_app.extensions["conversion_store"] = store
    return store


def _job_or_404(job_id: str) -> Job | tuple:
    try:
        normalized = str(UUID(job_id))
        job = _store().get_job(normalized)
    except (ValueError, TypeError, AttributeError):
        job = None
    if job is None:
        return jsonify(error="Conversion job not found."), 404
    return job


def _job_json(job: Job) -> dict:
    payload = {
        "id": job.id,
        "status": job.status.value,
        "language": job.language,
        "progress": {
            "completed_pages": job.completed_pages,
            "total_pages": job.total_pages,
            "current_page": job.current_page,
            "message": job.progress_message,
        },
        "error": (
            {"code": job.error_code, "message": job.error_message}
            if job.status is JobStatus.FAILED else None
        ),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "cancellation_requested": job.cancellation_requested,
        "links": {
            "self": url_for("conversion_api.get_conversion", job_id=job.id),
            "download": url_for("conversion_api.download_conversion", job_id=job.id),
            "report": url_for("conversion_api.download_report", job_id=job.id),
        },
    }
    return payload


def _completed_file(job: Job, stored_path: str | None, filename: str):
    if job.status is not JobStatus.COMPLETED:
        return None, (jsonify(error="The conversion has not completed."), 409)
    if not stored_path:
        return None, (jsonify(error="The requested output is not available."), 404)
    try:
        workspace = _store().workspace(job.id)
        root = workspace.output_dir.resolve(strict=True)
        path = Path(stored_path).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file() or path.is_symlink():
            raise ValueError("Not a regular output file")
    except (OSError, ValueError, RuntimeError):
        return None, (jsonify(error="The requested output is not available."), 404)
    return path, None


@conversion_api.post("")
def create_conversion():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(error="A PDF file is required in the 'file' field."), 400
    if Path(upload.filename).suffix.lower() != ".pdf":
        return jsonify(error="Only PDF files are supported for conversion."), 400

    try:
        language = normalize_language_selection(request.form.get("language", "auto"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    max_pages = current_app.config.get("CONVERSION_MAX_PDF_PAGES", 5000)
    with tempfile.TemporaryDirectory(prefix="inkslim-upload-") as temp_dir:
        source = Path(temp_dir) / "upload.pdf"
        upload.save(source)
        if source.stat().st_size == 0:
            return jsonify(error="The uploaded PDF is empty."), 422
        try:
            with open_pdf_document(source) as document:
                page_count = len(document)
                if page_count < 1:
                    return jsonify(error="The PDF does not contain any pages."), 422
                if max_pages and page_count > max_pages:
                    return jsonify(error=f"PDF exceeds maximum page limit of {max_pages}."), 422
        except PdfAnalysisError as exc:
            return jsonify(error=exc.public_message, code=exc.code), 422

        try:
            job = _store().create_job(source, Path(upload.filename).name, language)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception:
            logger.exception("Could not create PDF conversion job")
            return jsonify(error="The conversion job could not be created."), 500

    response = jsonify(_job_json(job))
    response.status_code = 201
    response.headers["Location"] = url_for("conversion_api.get_conversion", job_id=job.id)
    return response


@conversion_api.get("/<job_id>")
def get_conversion(job_id: str):
    job = _job_or_404(job_id)
    if isinstance(job, tuple):
        return job
    return jsonify(_job_json(job))


@conversion_api.delete("/<job_id>")
def cancel_conversion(job_id: str):
    job = _job_or_404(job_id)
    if isinstance(job, tuple):
        return job
    try:
        cancelled = _store().request_cancel(job.id)
    except JobNotFoundError:
        return jsonify(error="Conversion job not found."), 404
    except InvalidJobStateError:
        return jsonify(error="The conversion job cannot be cancelled in its current state."), 409
    updated = _store().get_job(job.id)
    response = jsonify(_job_json(updated))
    response.status_code = 200 if cancelled else 409
    return response


@conversion_api.get("/<job_id>/download")
def download_conversion(job_id: str):
    job = _job_or_404(job_id)
    if isinstance(job, tuple):
        return job
    path, error = _completed_file(job, job.output_path, "epub")
    if error:
        return error
    if validate_epub(path):
        logger.error("Completed conversion %s has an invalid EPUB output", job.id)
        return jsonify(error="The generated EPUB is not available."), 404
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job.input_name).stem).strip("._") or "book"
    return send_file(path, mimetype="application/epub+zip", as_attachment=True,
                     download_name=f"{stem}.epub", conditional=True)


@conversion_api.get("/<job_id>/report")
def download_report(job_id: str):
    job = _job_or_404(job_id)
    if isinstance(job, tuple):
        return job
    path, error = _completed_file(job, job.report_path, "report")
    if error:
        return error
    try:
        with path.open("r", encoding="utf-8") as stream:
            report = json.load(stream)
        if not isinstance(report, dict):
            raise ValueError("Report root must be an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return jsonify(error="The conversion report is not available."), 404
    return send_file(path, mimetype="application/json", as_attachment=True,
                     download_name="quality-report.json", conditional=True)
