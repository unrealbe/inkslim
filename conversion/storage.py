"""SQLite job queue and crash-safe on-disk workspaces/checkpoints."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .language import normalize_language_selection
from .models import (
    ACTIVE_JOB_STATUSES,
    Job,
    JobStatus,
    PageResult,
    UnsupportedCheckpointVersionError,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class JobNotFoundError(LookupError):
    pass


class InvalidJobStateError(RuntimeError):
    pass


class StorageQuotaExceededError(RuntimeError):
    pass


class JobWorkspace:
    """Persistent files for one job; page results are atomic JSON checkpoints."""

    def __init__(self, root: Path):
        self.root = root
        self.input_dir = root / "input"
        self.pages_dir = root / "pages"
        self.lines_dir = root / "lines"
        self.paragraphs_dir = root / "paragraphs"
        self.dehyphenated_dir = root / "dehyphenated"
        self.hyphenation_dir = root / "hyphenation"
        self.cleaned_dir = root / "cleaned"
        self.structure_dir = root / "structure"
        self.analysis_dir = root / "analysis"
        self.page_images_dir = root / "page_images"
        self.images_dir = root / "images"
        self.rendered_dir = root / "rendered"
        self.output_dir = root / "output"

    def create(self) -> None:
        for directory in (
            self.input_dir, self.analysis_dir, self.page_images_dir, self.images_dir, self.pages_dir, self.lines_dir, self.paragraphs_dir, self.dehyphenated_dir, self.hyphenation_dir, self.cleaned_dir, self.structure_dir, self.rendered_dir, self.output_dir
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def manifest_path(self) -> Path:
        return self.root / "job.json"

    def write_manifest(self, data: dict[str, Any]) -> None:
        self._atomic_json(self.manifest_path, data)

    def analysis_result_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.analysis_dir / f"{page_number:06d}.json"

    def save_page_analysis(self, analysis: Any) -> Path:
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        path = self.analysis_result_path(analysis.page_number)
        self._atomic_json(path, analysis.to_dict())
        return path

    def load_page_analysis(self, page_number: int) -> Any | None:
        from .pdf.models import PageAnalysis

        return self._load_checkpoint(
            self.analysis_result_path(page_number),
            PageAnalysis.from_dict,
            page_number,
            "analysis",
        )

    def page_images_checkpoint_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.page_images_dir / f"{page_number:06d}.json"

    def save_page_images(self, page_images: Any) -> Path:
        self.page_images_dir.mkdir(parents=True, exist_ok=True)
        path = self.page_images_checkpoint_path(page_images.page_number)
        self._atomic_json(path, page_images.to_dict())
        return path

    def load_page_images(self, page_number: int) -> Any | None:
        from .pdf.images import PageImages
        return self._load_checkpoint(
            self.page_images_checkpoint_path(page_number),
            PageImages.from_dict,
            page_number,
            "page-images",
        )

    def page_image_asset_prefix(self, page_number: int, image_index: int) -> Path:
        if page_number < 1 or image_index < 1:
            raise ValueError("page_number and image_index must be positive")
        return self.images_dir / f"{page_number:06d}-{image_index:04d}"

    def first_missing_analysis(self, total_pages: int) -> int:
        if total_pages < 0:
            raise ValueError("total_pages cannot be negative")
        for page_number in range(1, total_pages + 1):
            if self.load_page_analysis(page_number) is None:
                return page_number
        return total_pages + 1

    def page_result_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.pages_dir / f"{page_number:06d}.json"

    def save_page_result(self, result: PageResult) -> Path:
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        path = self.page_result_path(result.page_number)
        self._atomic_json(path, result.to_dict())
        return path

    def load_page_result(self, page_number: int) -> PageResult | None:
        return self._load_checkpoint(
            self.page_result_path(page_number),
            PageResult.from_dict,
            page_number,
            "page-result",
        )

    def page_lines_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.lines_dir / f"{page_number:06d}.json"

    def save_page_lines(self, page_number: int, lines: list[dict[str, Any]]) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        path = self.page_lines_path(page_number)
        self._atomic_json(path, {"schema_version": 1, "page_number": page_number, "lines": lines})
        return path

    def load_page_lines(self, page_number: int) -> list[dict[str, Any]] | None:
        path = self.page_lines_path(page_number)
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if value.get("schema_version") != 1 or value.get("page_number") != page_number:
                raise ValueError("Line checkpoint metadata does not match its page")
            lines = value.get("lines")
            if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
                raise ValueError("Line checkpoint has an invalid lines collection")
            return lines
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, page_number, "lines", "corrupt", exc)
            return None

    def _load_checkpoint(self, path: Path, loader: Any, page_number: int, kind: str) -> Any | None:
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            result = loader(value)
            if result.page_number != page_number:
                raise ValueError("Checkpoint page number does not match its filename")
            if value.get("schema_version", 0) == 0:
                # Persist the migrated current schema so this checkpoint is not
                # migrated again on every resume.
                self._atomic_json(path, result.to_dict())
                logger.info("Migrated legacy %s checkpoint for page %d", kind, page_number)
            return result
        except FileNotFoundError:
            return None
        except UnsupportedCheckpointVersionError as exc:
            self._quarantine_checkpoint(path, page_number, kind, "unknown-version", exc)
            return None
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
            self._quarantine_checkpoint(path, page_number, kind, "corrupt", exc)
            return None

    def _quarantine_checkpoint(
        self, path: Path, page_number: int, kind: str, reason: str, error: Exception
    ) -> None:
        if not path.exists():
            return
        quarantine_dir = path.parent / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = quarantine_dir / (
            f"{path.stem}.{reason}.{timestamp}.{uuid.uuid4().hex[:8]}{path.suffix}"
        )
        try:
            os.replace(path, target)
            logger.warning(
                "Ignoring %s checkpoint for page %d (%s); moved to quarantine: %s",
                kind,
                page_number,
                error,
                target.name,
            )
        except OSError:
            logger.exception(
                "Could not quarantine %s checkpoint for page %d", kind, page_number
            )

    def completed_analysis_count(self, total_pages: int) -> int:
        if total_pages < 0:
            raise ValueError("total_pages cannot be negative")
        return sum(
            self.load_page_analysis(page_number) is not None
            for page_number in range(1, total_pages + 1)
        )

    def first_missing_page(self, total_pages: int) -> int:
        if total_pages < 0:
            raise ValueError("total_pages cannot be negative")
        for page_number in range(1, total_pages + 1):
            if self.load_page_result(page_number) is None:
                return page_number
        return total_pages + 1

    def completed_page_count(self, total_pages: int) -> int:
        return sum(
            self.load_page_result(page_number) is not None
            for page_number in range(1, total_pages + 1)
        )


    def page_paragraphs_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.paragraphs_dir / f"{page_number:06d}.json"

    def save_page_paragraphs(
        self, page_number: int, paragraphs: list[dict[str, Any]]
    ) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        path = self.page_paragraphs_path(page_number)
        self._atomic_json(
            path,
            {
                "schema_version": 1,
                "page_number": page_number,
                "paragraphs": paragraphs,
            },
        )
        return path

    def load_page_paragraphs(self, page_number: int) -> list[dict[str, Any]] | None:
        path = self.page_paragraphs_path(page_number)
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if value.get("schema_version") != 1 or value.get("page_number") != page_number:
                raise ValueError("Paragraph checkpoint metadata does not match its page")
            paragraphs = value.get("paragraphs")
            if not isinstance(paragraphs, list) or any(
                not isinstance(paragraph, dict) for paragraph in paragraphs
            ):
                raise ValueError("Paragraph checkpoint has an invalid paragraphs collection")
            return paragraphs
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, page_number, "paragraphs", "corrupt", exc)
            return None


    def page_dehyphenated_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.dehyphenated_dir / f"{page_number:06d}.json"

    def save_page_dehyphenated(
        self, page_number: int, paragraphs: list[dict[str, Any]], *, removed_soft_hyphens: int
    ) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        if removed_soft_hyphens < 0:
            raise ValueError("removed_soft_hyphens cannot be negative")
        path = self.page_dehyphenated_path(page_number)
        self._atomic_json(
            path,
            {
                "schema_version": 1,
                "page_number": page_number,
                "removed_soft_hyphens": removed_soft_hyphens,
                "paragraphs": paragraphs,
            },
        )
        return path

    def load_page_dehyphenated(
        self, page_number: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        path = self.page_dehyphenated_path(page_number)
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if value.get("schema_version") != 1 or value.get("page_number") != page_number:
                raise ValueError("Dehyphenation checkpoint metadata does not match its page")
            paragraphs = value.get("paragraphs")
            removed = value.get("removed_soft_hyphens")
            if not isinstance(paragraphs, list) or any(
                not isinstance(paragraph, dict) for paragraph in paragraphs
            ):
                raise ValueError("Dehyphenation checkpoint has an invalid paragraphs collection")
            if type(removed) is not int or removed < 0:
                raise ValueError("Dehyphenation checkpoint has an invalid removal count")
            return paragraphs, removed
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, page_number, "dehyphenated", "corrupt", exc)
            return None

    def page_hyphenation_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.hyphenation_dir / f"{page_number:06d}.json"

    def save_page_hyphenation_candidates(
        self, page_number: int, candidates: list[dict[str, Any]]
    ) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        path = self.page_hyphenation_path(page_number)
        self._atomic_json(path, {
            "schema_version": 1,
            "page_number": page_number,
            "candidates": candidates,
        })
        return path

    def load_page_hyphenation_candidates(
        self, page_number: int
    ) -> list[dict[str, Any]] | None:
        path = self.page_hyphenation_path(page_number)
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if value.get("schema_version") != 1 or value.get("page_number") != page_number:
                raise ValueError("Hyphenation checkpoint metadata does not match its page")
            candidates = value.get("candidates")
            if not isinstance(candidates, list) or any(
                not isinstance(item, dict) for item in candidates
            ):
                raise ValueError("Hyphenation checkpoint has an invalid candidates collection")
            return candidates
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, page_number, "hyphenation", "corrupt", exc)
            return None

    @property
    def marginalia_analysis_path(self) -> Path:
        return self.output_dir / "marginalia-analysis.json"

    def save_marginalia_analysis(self, analysis: dict[str, Any]) -> Path:
        if analysis.get("schema_version") != 1:
            raise ValueError("Unsupported marginalia analysis schema version")
        path = self.marginalia_analysis_path
        self._atomic_json(path, analysis)
        return path

    def load_marginalia_analysis(self) -> dict[str, Any] | None:
        path = self.marginalia_analysis_path
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if value.get("schema_version") != 1 or not isinstance(value.get("items"), list):
                raise ValueError("Marginalia analysis checkpoint is invalid")
            if type(value.get("total_pages")) is not int or value["total_pages"] < 1:
                raise ValueError("Marginalia analysis page count is invalid")
            if not isinstance(value.get("removed"), list):
                raise ValueError("Marginalia removal decisions are invalid")
            return value
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, 1, "marginalia-analysis", "corrupt", exc)
            return None

    def page_cleaned_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.cleaned_dir / f"{page_number:06d}.json"

    def save_page_cleaned_paragraphs(
        self, page_number: int, paragraphs: list[dict[str, Any]]
    ) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        path = self.page_cleaned_path(page_number)
        self._atomic_json(path, {
            "schema_version": 1, "page_number": page_number, "paragraphs": paragraphs,
        })
        return path

    def load_page_cleaned_paragraphs(
        self, page_number: int
    ) -> list[dict[str, Any]] | None:
        path = self.page_cleaned_path(page_number)
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if value.get("schema_version") != 1 or value.get("page_number") != page_number:
                raise ValueError("Cleaned paragraph checkpoint metadata does not match its page")
            paragraphs = value.get("paragraphs")
            if not isinstance(paragraphs, list) or any(
                not isinstance(item, dict) for item in paragraphs
            ):
                raise ValueError("Cleaned paragraph checkpoint has an invalid collection")
            return paragraphs
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, page_number, "cleaned-paragraphs", "corrupt", exc)
            return None


    @property
    def structure_analysis_path(self) -> Path:
        return self.output_dir / "structure-analysis.json"

    def save_structure_analysis(self, analysis: dict[str, Any]) -> Path:
        if analysis.get("schema_version") != 1:
            raise ValueError("Unsupported structure analysis schema version")
        self._atomic_json(self.structure_analysis_path, analysis)
        return self.structure_analysis_path

    def load_structure_analysis(self) -> dict[str, Any] | None:
        path = self.structure_analysis_path
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if (
                value.get("schema_version") != 1
                or type(value.get("total_pages")) is not int
                or value["total_pages"] < 1
                or not isinstance(value.get("chapters"), list)
            ):
                raise ValueError("Structure analysis checkpoint is invalid")
            return value
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, 1, "structure-analysis", "corrupt", exc)
            return None

    @property
    def document_model_path(self) -> Path:
        return self.output_dir / "document-model.json"

    def save_document_model(self, model: Any) -> Path:
        data = model.to_dict() if hasattr(model, "to_dict") else model
        if data.get("schema_version") not in (1, 2):
            raise ValueError("Unsupported document model schema version")
        self._atomic_json(self.document_model_path, data)
        return self.document_model_path

    def load_document_model(self) -> Any | None:
        path = self.document_model_path
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if (
                value.get("schema_version") not in (1, 2)
                or not isinstance(value.get("chapters"), list)
            ):
                raise ValueError("Document model checkpoint is invalid")
            from .document import DocumentModel
            return DocumentModel.from_dict(value)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, 1, "document-model", "corrupt", exc)
            return None

    def page_structure_path(self, page_number: int) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        return self.structure_dir / f"{page_number:06d}.json"

    def save_page_structure(
        self, page_number: int, annotations: list[dict[str, Any]]
    ) -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        path = self.page_structure_path(page_number)
        self._atomic_json(path, {
            "schema_version": 1, "page_number": page_number, "annotations": annotations,
        })
        return path

    def load_page_structure(self, page_number: int) -> list[dict[str, Any]] | None:
        path = self.page_structure_path(page_number)
        try:
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            annotations = value.get("annotations")
            if (
                value.get("schema_version") != 1
                or value.get("page_number") != page_number
                or not isinstance(annotations, list)
                or any(not isinstance(item, dict) for item in annotations)
            ):
                raise ValueError("Page structure checkpoint is invalid")
            return annotations
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            self._quarantine_checkpoint(path, page_number, "page-structure", "corrupt", exc)
            return None

    def rendered_page_path(self, page_number: int, suffix: str = ".png") -> Path:
        if page_number < 1:
            raise ValueError("page_number must be at least 1")
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
            raise ValueError("suffix must be a simple file extension")
        return self.rendered_dir / f"{page_number:06d}{suffix.lower()}"

    def remove_rendered_page(self, page_number: int, suffix: str = ".png") -> None:
        self.rendered_page_path(page_number, suffix).unlink(missing_ok=True)

    @staticmethod
    def _atomic_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            # Persist the directory entry as well as the file contents on Linux filesystems.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


class JobStore:
    """SQLite-backed queue. Keep its database and job files on local NAS storage."""

    def __init__(self, database_path: Path, jobs_dir: Path, *, max_storage_bytes: int = 5 * 1024**3, retention_hours: int = 24):
        self.database_path = Path(database_path)
        self.jobs_dir = Path(jobs_dir)
        if max_storage_bytes < 1:
            raise ValueError('max_storage_bytes must be positive')
        self.max_storage_bytes = max_storage_bytes
        if retention_hours < 1:
            raise ValueError('retention_hours must be at least 1')
        self.retention_hours = retention_hours
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            self._migrate_schema(connection)

    @staticmethod
    def _create_jobs_table(connection: sqlite3.Connection, table_name: str) -> None:
        if table_name not in {"jobs", "jobs_v2"}:
            raise ValueError("Invalid internal table name")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN (
                    'queued', 'analyzing', 'ready_for_ocr', 'extracting', 'ocr',
                    'postprocessing', 'building_epub', 'validating', 'completed',
                    'failed', 'cancelled'
                )),
                input_name TEXT NOT NULL,
                input_path TEXT NOT NULL,
                output_path TEXT,
                report_path TEXT,
                language TEXT NOT NULL,
                total_pages INTEGER,
                completed_pages INTEGER NOT NULL DEFAULT 0,
                current_page INTEGER,
                progress_message TEXT,
                cancellation_requested INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                expires_at TEXT
            )
            """
        )

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        """Apply transactional schema migrations tracked by SQLite user_version.

        Version 0 is the unversioned schema shipped by earlier releases. On first
        startup it is adopted into version 1. Existing jobs tables are rebuilt to
        guarantee the current CHECK constraint; fresh databases get the current
        table directly. Later migrations must increment SCHEMA_VERSION.
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if version == SCHEMA_VERSION:
                connection.commit()
                return

            if version == 0:
                existing_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
                ).fetchone()
                if existing_table is None:
                    self._create_jobs_table(connection, "jobs")
                else:
                    # Adopt an unversioned installation. Rebuilding is safe for both
                    # the older CHECK constraint and databases already on the new
                    # schema, and preserves every persisted job row.
                    columns = (
                        "id, status, input_name, input_path, output_path, report_path, language, "
                        "total_pages, completed_pages, current_page, progress_message, "
                        "cancellation_requested, worker_id, error_code, error_message, created_at, "
                        "updated_at, started_at, completed_at, expires_at"
                    )
                    connection.execute("DROP INDEX IF EXISTS idx_jobs_queue")
                    connection.execute("DROP TABLE IF EXISTS jobs_v2")
                    self._create_jobs_table(connection, "jobs_v2")
                    connection.execute(
                        f"INSERT INTO jobs_v2 ({columns}) SELECT {columns} FROM jobs"
                    )
                    connection.execute("DROP TABLE jobs")
                    connection.execute("ALTER TABLE jobs_v2 RENAME TO jobs")

                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at)"
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path, timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def workspace(self, job_id: str) -> JobWorkspace:
        try:
            normalized_id = str(uuid.UUID(job_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid job ID") from exc
        return JobWorkspace(self.jobs_dir / normalized_id)

    def _safe_job_directory(self, job_id: str) -> Path:
        normalized = str(uuid.UUID(job_id))
        root = self.jobs_dir.resolve()
        candidate = self.jobs_dir / normalized
        if candidate.is_symlink() or candidate.parent.resolve() != root:
            raise ValueError("Unsafe job workspace path")
        return candidate

    def storage_usage_bytes(self) -> int:
        total = 0
        for child in self.jobs_dir.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            for path in child.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def cleanup_expired_jobs(self, retention_hours: int | None = None) -> int:
        retention_hours = retention_hours if retention_hours is not None else self.retention_hours
        if retention_hours < 1:
            raise ValueError("retention_hours must be at least 1")
        cutoff = (datetime.now(UTC) - timedelta(hours=retention_hours)).isoformat(timespec="seconds")
        removed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status IN (?, ?, ?) AND completed_at IS NOT NULL AND completed_at <= ?",
                (JobStatus.COMPLETED.value, JobStatus.FAILED.value,
                 JobStatus.CANCELLED.value, cutoff),
            ).fetchall()
            for row in rows:
                directory = self._safe_job_directory(row["id"])
                if directory.exists():
                    shutil.rmtree(directory)
                connection.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
                removed += 1
            connection.commit()
        if removed:
            logger.info("Removed %d expired conversion job(s)", removed)
        return removed

    def create_job(
        self,
        source_path: Path,
        input_name: str,
        language: str = "auto",
        *,
        expires_in: timedelta | None = None,
    ) -> Job:
        source = Path(source_path).resolve(strict=True)
        if not source.is_file():
            raise ValueError("Input must be a file")
        if source.suffix.lower() != ".pdf":
            raise ValueError("Conversion V1 currently accepts PDF input only")
        language = normalize_language_selection(language)

        job_id = str(uuid.uuid4())
        workspace = self.workspace(job_id)
        workspace.create()
        input_path = workspace.input_dir / "original.pdf"
        now = self._now()
        expires_at = (datetime.now(UTC) + expires_in).isoformat(timespec="seconds") if expires_in else None

        try:
            shutil.copyfile(source, input_path)
            workspace.write_manifest(
                {
                    "schema_version": 1,
                    "id": job_id,
                    "input_name": input_name or source.name,
                    "input_path": str(input_path),
                    "language": language,
                    "created_at": now,
                }
            )
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self.storage_usage_bytes() > self.max_storage_bytes:
                    raise StorageQuotaExceededError(
                        "Conversion storage is full. Remove older jobs or increase MAX_CONVERSION_STORAGE_MB."
                    )
                connection.execute(
                    """INSERT INTO jobs
                       (id, status, input_name, input_path, language, created_at,
                        updated_at, expires_at, progress_message)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        JobStatus.QUEUED.value,
                        input_name or source.name,
                        str(input_path),
                        language,
                        now,
                        now,
                        expires_at,
                        "Waiting for conversion worker",
                    ),
                )
                connection.commit()
        except Exception:
            shutil.rmtree(workspace.root, ignore_errors=True)
            raise

        job = self.get_job(job_id)
        assert job is not None
        return job

    def get_job(self, job_id: str) -> Job | None:
        normalized_id = str(uuid.UUID(job_id))
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (normalized_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[Job]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Job.from_row(row) for row in rows]

    def delete_job(self, job_id: str) -> bool:
        """Permanently remove one terminal job and its complete workspace."""
        normalized_id = str(uuid.UUID(job_id))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_job(connection, normalized_id)
            if row is None:
                connection.rollback()
                return False
            status = JobStatus(row["status"])
            if status not in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }:
                connection.rollback()
                raise InvalidJobStateError(
                    "Only completed, failed or cancelled jobs can be deleted"
                )
            directory = self._safe_job_directory(normalized_id)
            if directory.exists():
                shutil.rmtree(directory)
            connection.execute("DELETE FROM jobs WHERE id = ?", (normalized_id,))
            connection.commit()
        return True

    def clear_terminal_jobs(self) -> int:
        """Permanently remove terminal jobs while retaining queued and active jobs."""
        terminal = (
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        )
        removed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status IN (?, ?, ?)", terminal
            ).fetchall()
            for row in rows:
                directory = self._safe_job_directory(row["id"])
                if directory.exists():
                    shutil.rmtree(directory)
                connection.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
                removed += 1
            connection.commit()
        if removed:
            logger.info("Manually removed %d terminal conversion job(s)", removed)
        return removed

    def claim_next_job(self, worker_id: str) -> Job | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be a non-empty identifier of at most 128 characters")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT id, status FROM jobs
                   WHERE status IN (?, ?) ORDER BY created_at, id LIMIT 1""",
                (JobStatus.QUEUED.value, JobStatus.READY_FOR_OCR.value),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = self._now()
            claimed_status = (
                JobStatus.ANALYZING
                if JobStatus(row["status"]) is JobStatus.QUEUED
                else JobStatus.OCR
            )
            connection.execute(
                """UPDATE jobs SET status = ?, worker_id = ?, started_at = COALESCE(started_at, ?),
                   updated_at = ?, progress_message = ? WHERE id = ? AND status = ?""",
                (
                    claimed_status.value,
                    worker_id,
                    now,
                    now,
                    "Worker claimed job",
                    row["id"],
                    row["status"],
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            connection.commit()
        return Job.from_row(claimed) if claimed else None

    def recover_interrupted_jobs(self) -> int:
        """Requeue active jobs after a worker restart; page checkpoints remain untouched."""
        statuses = tuple(status.value for status in ACTIVE_JOB_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""UPDATE jobs
                    SET status = CASE
                            WHEN cancellation_requested = 1 THEN ?
                            WHEN status = ? THEN ?
                            ELSE ? END,
                        worker_id = NULL, updated_at = ?,
                        completed_at = CASE WHEN cancellation_requested = 1 THEN ? ELSE completed_at END,
                        progress_message = CASE WHEN cancellation_requested = 1
                            THEN 'Cancelled during worker restart'
                            ELSE 'Resuming after worker restart' END
                    WHERE status IN ({placeholders})""",
                (JobStatus.CANCELLED.value, JobStatus.OCR.value, JobStatus.READY_FOR_OCR.value, JobStatus.QUEUED.value, now, now, *statuses),
            )
            connection.commit()
        return cursor.rowcount

    def reconcile_analysis_progress(
        self,
        job_id: str,
        *,
        total_pages: int,
        completed_pages: int,
        next_page: int | None,
    ) -> Job:
        """Make SQLite's cached progress exactly match valid on-disk checkpoints."""
        if total_pages < 1:
            raise ValueError("total_pages must be at least 1")
        if not 0 <= completed_pages <= total_pages:
            raise ValueError("completed_pages must be within the document page count")
        if next_page is not None and not 1 <= next_page <= total_pages:
            raise ValueError("next_page must be within the document page count")
        now = self._now()
        message = (
            f"Resuming PDF analysis at page {next_page}"
            if next_page is not None
            else "All PDF pages are already analyzed"
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET status = ?, total_pages = ?, completed_pages = ?,
                   current_page = ?, progress_message = ?, updated_at = ?
                   WHERE id = ? AND status = ?""",
                (
                    JobStatus.ANALYZING.value,
                    total_pages,
                    completed_pages,
                    next_page,
                    message,
                    now,
                    job_id,
                    JobStatus.ANALYZING.value,
                ),
            )
            if cursor.rowcount == 0:
                row = self._select_job(connection, job_id)
                if row is None:
                    raise JobNotFoundError(job_id)
                if JobStatus(row["status"]) is not JobStatus.ANALYZING:
                    raise InvalidJobStateError("Job is not in the analyzing phase")
        job = self.get_job(job_id)
        assert job is not None
        return job

    def mark_ready_for_ocr(self, job_id: str) -> bool:
        """Finish analysis phase without marking the overall conversion completed."""
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_job(connection, job_id)
            if row is None:
                raise JobNotFoundError(job_id)
            if JobStatus(row["status"]) is not JobStatus.ANALYZING:
                raise InvalidJobStateError("Only an analyzing job can become ready for OCR")
            if row["cancellation_requested"]:
                connection.execute(
                    """UPDATE jobs SET status = ?, completed_at = ?, updated_at = ?,
                       worker_id = NULL, progress_message = ? WHERE id = ?""",
                    (JobStatus.CANCELLED.value, now, now, "Job cancelled", job_id),
                )
                connection.commit()
                return False
            connection.execute(
                """UPDATE jobs SET status = ?, worker_id = NULL, current_page = NULL,
                   completed_at = NULL, progress_message = ?, updated_at = ? WHERE id = ?""",
                (JobStatus.READY_FOR_OCR.value, "PDF analysis completed", now, job_id),
            )
            connection.commit()
        return True

    def mark_extracting(
        self, job_id: str, *, report_path: Path | str | None = None
    ) -> bool:
        """Finish OCR, persist its quality report path and await text post-processing."""
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_job(connection, job_id)
            if row is None:
                raise JobNotFoundError(job_id)
            if JobStatus(row["status"]) is not JobStatus.OCR:
                raise InvalidJobStateError("Only an OCR job can become extracting")
            if row["cancellation_requested"]:
                connection.execute(
                    """UPDATE jobs SET status = ?, completed_at = ?, updated_at = ?,
                       worker_id = NULL, progress_message = ? WHERE id = ?""",
                    (JobStatus.CANCELLED.value, now, now, "Job cancelled", job_id),
                )
                connection.commit()
                return False
            connection.execute(
                """UPDATE jobs SET status = ?, report_path = COALESCE(?, report_path),
                   worker_id = NULL, current_page = NULL, completed_at = NULL,
                   progress_message = ?, updated_at = ? WHERE id = ?""",
                (
                    JobStatus.EXTRACTING.value,
                    str(report_path) if report_path is not None else None,
                    "OCR completed; quality report saved; ready for text post-processing",
                    now,
                    job_id,
                ),
            )
            connection.commit()
        return True

    def resume_cancelled_job(self, job_id: str) -> Job:
        """Explicitly requeue a cancelled job, retaining its durable checkpoints."""
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_job(connection, job_id)
            if row is None:
                raise JobNotFoundError(job_id)
            if JobStatus(row["status"]) is not JobStatus.CANCELLED:
                raise InvalidJobStateError("Only a cancelled job can be resumed")
            connection.execute(
                """UPDATE jobs SET status = ?, cancellation_requested = 0,
                   worker_id = NULL, error_code = NULL, error_message = NULL,
                   completed_at = NULL, updated_at = ?, progress_message = ? WHERE id = ?""",
                (JobStatus.QUEUED.value, now, "Queued for explicit resume", job_id),
            )
            connection.commit()
        job = self.get_job(job_id)
        assert job is not None
        return job

    def update_progress(
        self,
        job_id: str,
        *,
        status: JobStatus,
        current_page: int | None = None,
        total_pages: int | None = None,
        completed_pages: int | None = None,
        message: str | None = None,
    ) -> Job:
        status = JobStatus(status)
        if status not in ACTIVE_JOB_STATUSES:
            raise ValueError("progress status must be an active job status")
        if current_page is not None and current_page < 1:
            raise ValueError("current_page must be at least 1")
        if total_pages is not None and total_pages < 1:
            raise ValueError("total_pages must be at least 1")
        if completed_pages is not None and completed_pages < 0:
            raise ValueError("completed_pages cannot be negative")
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET status = ?, current_page = COALESCE(?, current_page),
                   total_pages = COALESCE(?, total_pages),
                   completed_pages = CASE WHEN ? IS NULL THEN completed_pages ELSE MAX(completed_pages, ?) END,
                   progress_message = COALESCE(?, progress_message), updated_at = ?
                   WHERE id = ? AND status NOT IN (?, ?, ?)""",
                (
                    status.value,
                    current_page,
                    total_pages,
                    completed_pages,
                    completed_pages,
                    message,
                    now,
                    job_id,
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ),
            )
            if cursor.rowcount == 0:
                if self._select_job(connection, job_id) is None:
                    raise JobNotFoundError(job_id)
                raise InvalidJobStateError("Cannot update progress for a terminal job")
        job = self.get_job(job_id)
        assert job is not None
        return job

    @staticmethod
    def _select_job(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def request_cancel(self, job_id: str) -> bool:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_job(connection, job_id)
            if row is None:
                raise JobNotFoundError(job_id)
            status = JobStatus(row["status"])
            if status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                connection.commit()
                return False
            if status in {JobStatus.QUEUED, JobStatus.READY_FOR_OCR}:
                connection.execute(
                    """UPDATE jobs SET status = ?, completed_at = ?, updated_at = ?,
                       progress_message = ? WHERE id = ?""",
                    (JobStatus.CANCELLED.value, now, now, "Cancelled before processing", job_id),
                )
            else:
                connection.execute(
                    """UPDATE jobs SET cancellation_requested = 1, updated_at = ?,
                       progress_message = ? WHERE id = ?""",
                    (now, "Cancellation requested; stopping after the current page", job_id),
                )
            connection.commit()
        return True

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancellation_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return bool(row[0])

    def mark_cancelled(self, job_id: str, message: str = "Job cancelled") -> None:
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET status = ?, completed_at = ?, updated_at = ?,
                   worker_id = NULL, progress_message = ?
                   WHERE id = ? AND status NOT IN (?, ?, ?)""",
                (
                    JobStatus.CANCELLED.value,
                    now,
                    now,
                    message,
                    job_id,
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ),
            )
            if cursor.rowcount == 0:
                if self._select_job(connection, job_id) is None:
                    raise JobNotFoundError(job_id)

    def complete_job(
        self,
        job_id: str,
        *,
        output_path: Path | str | None = None,
        report_path: Path | str | None = None,
    ) -> bool:
        """Complete a job unless a cancellation request won the final race."""
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_job(connection, job_id)
            if row is None:
                raise JobNotFoundError(job_id)
            if JobStatus(row["status"]) in {
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED
            }:
                raise InvalidJobStateError("Job is already terminal")
            if JobStatus(row["status"]) not in ACTIVE_JOB_STATUSES:
                raise InvalidJobStateError("Only an active job can be completed")

            if row["cancellation_requested"]:
                connection.execute(
                    """UPDATE jobs SET status = ?, completed_at = ?, updated_at = ?,
                       worker_id = NULL, progress_message = ? WHERE id = ?""",
                    (JobStatus.CANCELLED.value, now, now, "Job cancelled", job_id),
                )
                connection.commit()
                return False

            connection.execute(
                """UPDATE jobs SET status = ?, output_path = ?, report_path = ?,
                   completed_at = ?, updated_at = ?, worker_id = NULL,
                   progress_message = ? WHERE id = ?""",
                (
                    JobStatus.COMPLETED.value,
                    str(output_path) if output_path is not None else None,
                    str(report_path) if report_path is not None else None,
                    now,
                    now,
                    "Conversion completed",
                    job_id,
                ),
            )
            connection.commit()
        return True

    def fail_job(self, job_id: str, error_code: str, error_message: str) -> None:
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET status = ?, error_code = ?, error_message = ?,
                   completed_at = ?, updated_at = ?, worker_id = NULL,
                   progress_message = ?
                   WHERE id = ? AND status NOT IN (?, ?, ?)""",
                (
                    JobStatus.FAILED.value,
                    error_code[:100],
                    error_message[:2000],
                    now,
                    now,
                    "Conversion failed",
                    job_id,
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ),
            )
            if cursor.rowcount == 0:
                if self._select_job(connection, job_id) is None:
                    raise JobNotFoundError(job_id)
