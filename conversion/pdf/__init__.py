"""PDF-specific analysis, native text extraction and guarded rendering."""

from .analyzer import PdfAnalysisError, analyze_pdf, analyze_pdf_page, open_pdf_document
from .processor import PdfAnalysisJobProcessor
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
    "PdfAnalysisJobProcessor",
    "RenderPlan",
    "RenderPlanError",
    "analyze_pdf",
    "analyze_pdf_page",
    "open_pdf_document",
    "build_render_plan",
    "render_pdf_page",
]
