"""Per-page PDF analysis to choose native text, OCR, or empty-page handling."""

from __future__ import annotations

import logging
from pathlib import Path

import pypdfium2 as pdfium

from .models import PageAnalysis, PageDecision
from .native_text import extract_native_text
from .quality import assess_native_text
from .renderer import PDFRenderLimits, RenderPlanError, build_render_plan

logger = logging.getLogger(__name__)


class PdfAnalysisError(RuntimeError):
    """Safe, user-facing PDF analysis failure with an internal error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


def analyze_pdf(
    source: Path,
    *,
    render_limits: PDFRenderLimits | None = None,
) -> list[PageAnalysis]:
    """Analyze every PDF page independently; never infer a document-wide route."""
    source = Path(source)
    if not source.is_file():
        raise PdfAnalysisError("INPUT_NOT_FOUND", "The uploaded PDF could not be found")

    limits = render_limits or PDFRenderLimits()
    results: list[PageAnalysis] = []

    try:
        with pdfium.PdfDocument(str(source)) as document:
            total_pages = len(document)
            if total_pages < 1:
                raise PdfAnalysisError("EMPTY_PDF", "The PDF does not contain any pages")

            for page_index in range(total_pages):
                page_number = page_index + 1
                page = document[page_index]
                try:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise ValueError("invalid page dimensions")

                    native = extract_native_text(page)
                    try:
                        image_count = sum(
                            1
                            for _ in page.get_objects(
                                filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE],
                                max_depth=15,
                            )
                        )
                    except Exception:
                        image_count = 0
                        logger.warning(
                            "Could not count image objects on PDF page %d", page_number,
                            exc_info=True,
                        )
                        image_scan_failed = True
                    else:
                        image_scan_failed = False

                    assessment = assess_native_text(
                        native.text,
                        image_count=image_count,
                        geometry_usable_ratio=native.geometry_usable_ratio,
                    )
                    warnings = list(assessment.warnings)
                    decision = assessment.decision
                    if image_scan_failed and not native.text.strip():
                        # When image enumeration fails, do not misclassify an empty text
                        # extraction as a genuinely blank page.
                        decision = PageDecision.OCR_REQUIRED
                        warnings.append("IMAGE_OBJECT_SCAN_FAILED")
                    render_dpi = None
                    render_width_px = None
                    render_height_px = None

                    if decision is PageDecision.OCR_REQUIRED:
                        try:
                            plan = build_render_plan(
                                width,
                                height,
                                rotation=int(page.get_rotation()),
                                limits=limits,
                            )
                            render_dpi = plan.actual_dpi
                            render_width_px = plan.width_px
                            render_height_px = plan.height_px
                            warnings.extend(plan.warnings)
                        except RenderPlanError as exc:
                            warnings.append("RENDER_LIMIT_EXCEEDED")
                            logger.warning(
                                "PDF page %d requires OCR but cannot be rendered safely: %s",
                                page_number,
                                exc,
                            )

                    results.append(
                        PageAnalysis(
                            page_number=page_number,
                            width=width,
                            height=height,
                            character_count=len(native.text),
                            word_count=assessment.word_count,
                            image_count=image_count,
                            has_native_text=bool(native.text.strip()),
                            native_text_quality=assessment.quality,
                            decision=decision,
                            meaningful_character_ratio=assessment.meaningful_character_ratio,
                            control_character_ratio=assessment.control_character_ratio,
                            replacement_character_ratio=assessment.replacement_character_ratio,
                            single_character_word_ratio=assessment.single_character_word_ratio,
                            geometry_usable_ratio=native.geometry_usable_ratio,
                            meaningful_character_count=assessment.meaningful_character_count,
                            render_dpi=render_dpi,
                            render_width_px=render_width_px,
                            render_height_px=render_height_px,
                            warnings=warnings,
                        )
                    )
                except PdfAnalysisError:
                    raise
                except Exception as exc:
                    raise PdfAnalysisError(
                        "PAGE_ANALYSIS_FAILED",
                        f"Could not analyze PDF page {page_number}",
                    ) from exc
                finally:
                    page.close()
    except PdfAnalysisError:
        raise
    except pdfium.PdfiumError as exc:
        password_error = getattr(pdfium.raw, "FPDF_ERR_PASSWORD", 4)
        if getattr(exc, "err_code", None) == password_error:
            raise PdfAnalysisError(
                "PASSWORD_PROTECTED",
                "Password-protected PDFs are not supported. Remove the password first.",
            ) from exc
        raise PdfAnalysisError(
            "INVALID_PDF",
            "This does not appear to be a valid or readable PDF.",
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise PdfAnalysisError(
            "INVALID_PDF",
            "This does not appear to be a valid or readable PDF.",
        ) from exc

    return results
