"""OCR engine adapters for PDF conversion."""

from .tesseract import OcrEngine, OcrError, TesseractOcrEngine

__all__ = ["OcrEngine", "OcrError", "TesseractOcrEngine"]
