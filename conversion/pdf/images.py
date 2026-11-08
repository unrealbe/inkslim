"""Durable extraction of embedded PDF images and their page geometry."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pypdfium2 as pdfium
from PIL import Image

logger = logging.getLogger(__name__)
IMAGE_OBJECT_FILTER = [pdfium.raw.FPDF_PAGEOBJ_IMAGE]
MAX_IMAGE_PIXELS = 50_000_000
MAX_IMAGE_DIMENSION = 12_000


@dataclass(frozen=True, slots=True)
class PageImage:
    """One extracted PDF image in normalized top-left page coordinates."""
    page_number: int
    image_index: int
    bbox: tuple[float, float, float, float]
    width: int
    height: int
    media_type: str
    source_path: str
    coverage: float

    def __post_init__(self) -> None:
        if self.page_number < 1 or self.image_index < 1:
            raise ValueError("page_number and image_index must be positive")
        if len(self.bbox) != 4:
            raise ValueError("bbox must contain four coordinates")
        bbox = tuple(float(value) for value in self.bbox)
        x0, y0, x1, y1 = bbox
        if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
            raise ValueError("bbox coordinates must be normalized and ordered")
        object.__setattr__(self, "bbox", bbox)
        if self.width < 1 or self.height < 1:
            raise ValueError("image dimensions must be positive")
        if self.media_type not in {"image/jpeg", "image/png"}:
            raise ValueError("unsupported media type")
        relative = PurePosixPath(self.source_path)
        if relative.is_absolute() or ".." in relative.parts or not self.source_path.startswith("images/"):
            raise ValueError("source_path must be a safe relative images path")
        if not 0 <= self.coverage <= 1:
            raise ValueError("coverage must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PageImage:
        return cls(int(value["page_number"]), int(value["image_index"]),
                   tuple(value["bbox"]), int(value["width"]), int(value["height"]),
                   str(value["media_type"]), str(value["source_path"]),
                   float(value["coverage"]))


@dataclass(frozen=True, slots=True)
class PageImages:
    """Per-page image extraction checkpoint."""
    page_number: int
    images: tuple[PageImage, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number must be positive")
        if any(image.page_number != self.page_number for image in self.images):
            raise ValueError("image page number does not match checkpoint")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "page_number": self.page_number,
                "images": [image.to_dict() for image in self.images],
                "warnings": list(self.warnings)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PageImages:
        if value.get("schema_version") != 1:
            raise ValueError("Unsupported page-image checkpoint schema")
        return cls(int(value["page_number"]),
                   tuple(PageImage.from_dict(item) for item in value.get("images", [])),
                   tuple(str(item) for item in value.get("warnings", [])))


def _normalized_bbox(page, image) -> tuple[float, float, float, float]:
    pl, pb, pr, pt = (float(v) for v in page.get_bbox())
    left, bottom, right, top = (float(v) for v in image.get_bounds())
    if pr <= pl or pt <= pb or right <= left or top <= bottom:
        raise ValueError("invalid image/page geometry")
    x0 = min(1.0, max(0.0, (left - pl) / (pr - pl)))
    x1 = min(1.0, max(0.0, (right - pl) / (pr - pl)))
    y0 = min(1.0, max(0.0, (pt - top) / (pt - pb)))
    y1 = min(1.0, max(0.0, (pt - bottom) / (pt - pb)))
    return x0, y0, x1, y1


def _remove_candidates(prefix: Path) -> None:
    for path in prefix.parent.glob(prefix.name + ".*"):
        path.unlink(missing_ok=True)


def _extract_supported_image(image, prefix: Path) -> tuple[Path, str, int, int]:
    image.extract(str(prefix), fb_format="png")
    candidates = [p for p in prefix.parent.glob(prefix.name + ".*") if p.is_file()]
    asset = next((p for p in candidates if p.suffix.lower() in {".jpg", ".jpeg", ".png"}), None)
    if asset is not None:
        media_type = "image/jpeg" if asset.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        with Image.open(asset) as im:
            im.verify()
        with Image.open(asset) as im:
            width, height = im.size
        return asset, media_type, width, height

    # PDFium rendering applies masks/transforms for formats we do not package directly.
    _remove_candidates(prefix)
    with image.get_bitmap(render=True) as bitmap:
        rendered = bitmap.to_pil().copy()
    asset = prefix.with_suffix(".png")
    rendered.save(asset, format="PNG", optimize=True)
    width, height = rendered.size
    rendered.close()
    return asset, "image/png", width, height


def extract_page_images(page, page_number: int, workspace) -> PageImages:
    """Extract page image objects; an individual image failure never drops page text."""
    try:
        objects = list(page.get_objects(filter=IMAGE_OBJECT_FILTER, max_depth=15))
    except Exception:
        logger.warning("Could not enumerate PDF images on page %d", page_number, exc_info=True)
        return PageImages(page_number, warnings=("IMAGE_OBJECT_SCAN_FAILED",))

    workspace.images_dir.mkdir(parents=True, exist_ok=True)
    results: list[PageImage] = []
    warnings: list[str] = []
    for image_index, image in enumerate(objects, start=1):
        prefix = workspace.page_image_asset_prefix(page_number, image_index)
        try:
            px_width, px_height = (int(v) for v in image.get_px_size())
            if (px_width < 1 or px_height < 1 or px_width * px_height > MAX_IMAGE_PIXELS
                    or max(px_width, px_height) > MAX_IMAGE_DIMENSION):
                warnings.append(f"IMAGE_PIXEL_LIMIT_EXCEEDED:{image_index}")
                continue
            bbox = _normalized_bbox(page, image)
            asset, media_type, width, height = _extract_supported_image(image, prefix)
            if width * height > MAX_IMAGE_PIXELS or max(width, height) > MAX_IMAGE_DIMENSION:
                asset.unlink(missing_ok=True)
                warnings.append(f"IMAGE_PIXEL_LIMIT_EXCEEDED:{image_index}")
                continue
            x0, y0, x1, y1 = bbox
            results.append(PageImage(
                page_number, image_index, bbox, width, height, media_type,
                asset.relative_to(workspace.root).as_posix(), (x1-x0)*(y1-y0),
            ))
        except Exception as exc:
            logger.warning("Could not extract image %d from PDF page %d: %s",
                           image_index, page_number, exc, exc_info=True)
            _remove_candidates(prefix)
            warnings.append(f"IMAGE_EXTRACTION_FAILED:{image_index}")
    return PageImages(page_number, tuple(results), tuple(warnings))
