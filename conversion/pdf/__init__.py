"""PDF-specific analysis, native text extraction and guarded rendering."""

from .analyzer import PdfAnalysisError, analyze_pdf
from .models import PageAnalysis, PageDecision
from .renderer import (
    PDFRenderLimits,
    RenderPlan,
    RenderPlanError,
    build_render_plan,
    render_pdf_page,
)

__all__ = [
    "PageAnalysis",
    "PageDecision",
    "PDFRenderLimits",
    "PdfAnalysisError",
    "RenderPlan",
    "RenderPlanError",
    "analyze_pdf",
    "build_render_plan",
    "render_pdf_page",
]
