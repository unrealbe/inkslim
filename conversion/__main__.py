"""Command-line entrypoint for the dedicated conversion worker service."""

from __future__ import annotations

import logging
import os

from .config import ConversionConfig
from .pdf.ocr_processor import PdfConversionJobProcessor
from .pdf.renderer import PDFRenderLimits
from .storage import JobStore
from .worker import ConversionWorker


def main() -> None:
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = ConversionConfig.from_env()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(config.database_path, config.jobs_dir)
    render_limits = PDFRenderLimits(
        dpi=config.pdf_render_dpi,
        minimum_dpi=config.min_render_dpi,
        max_pixels=config.max_render_pixels,
        max_dimension=config.max_render_dimension,
    )
    processor = PdfConversionJobProcessor(
        render_limits=render_limits,
        timeout_seconds=config.ocr_timeout_page,
    )
    worker = ConversionWorker(
        store,
        processor,
        poll_seconds=config.queue_poll_seconds,
    )
    logging.getLogger(__name__).info(
        "Starting conversion worker (data_dir=%s, poll_seconds=%s)",
        config.data_dir,
        config.queue_poll_seconds,
    )
    worker.run_forever()


if __name__ == "__main__":
    main()
