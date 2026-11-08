"""Opt-in, page-scoped diagnostics for the PDF text pipeline."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from .lines import ReconstructedLine
from .models import PageResult
from .paragraphs import ReconstructedParagraph


def debug_page_requested(page_number: int) -> bool:
    """Return whether this page was selected through DEBUG_CONVERSION_PAGE."""
    configured = os.environ.get("DEBUG_CONVERSION_PAGE", "").strip()
    if not configured:
        return False
    try:
        selected_page = int(configured)
    except ValueError:
        return False
    return selected_page > 0 and page_number == selected_page


def dump_page_pipeline(
    result: PageResult,
    lines: Sequence[ReconstructedLine],
    paragraphs: Sequence[ReconstructedParagraph],
    output_path: Path,
) -> None:
    """Atomically write the three relevant representations for one PDF page."""
    payload = {
        "page": result.page_number,
        "page_result": {
            "source": result.source.value,
            "quality": result.quality.value,
            "blocks": [
                {"index": index, **block.to_dict()}
                for index, block in enumerate(result.blocks)
            ],
        },
        "reconstructed_lines": [line.to_dict() for line in lines],
        "paragraphs": [paragraph.to_dict() for paragraph in paragraphs],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary_path.replace(output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
