"""Place extracted PDF images beside their source text when geometry is unambiguous.

Images that cannot be anchored safely remain in a labelled appendix. This module
never changes text order and never guesses across overlapping text/image regions.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Mapping
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from PIL import Image

from ..pdf.images import MAX_IMAGE_DIMENSION, MAX_IMAGE_PIXELS, PageImage
from ..storage import JobWorkspace

_OPF_NS = "http://www.idpf.org/2007/opf"
_XHTML_END = b"</section>"
ET.register_namespace("", _OPF_NS)
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
_PARAGRAPH_RE = re.compile(
    rb'<(p|h2|h3)\b([^>]*)>(.*?)</\1>', re.DOTALL
)
_SOURCE_PAGE_RE = re.compile(rb'\bdata-source-page="([0-9]+)"')
_SOURCE_BBOX_RE = re.compile(rb'\bdata-source-bbox="([0-9.,-]+)"')
_ID_RE = re.compile(rb'\bid="(p-[0-9]+-[0-9]+)"')


class EpubImageEmbeddingError(ValueError):
    """Raised when extracted image assets cannot safely be embedded."""


def _load_assets(
    workspace: JobWorkspace, total_pages: int
) -> list[tuple[PageImage, Path]]:
    root = workspace.root.resolve()
    assets: list[tuple[PageImage, Path]] = []
    for page_number in range(1, total_pages + 1):
        checkpoint = workspace.load_page_images(page_number)
        if checkpoint is None:
            continue
        for image in checkpoint.images:
            relative = Path(image.source_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise EpubImageEmbeddingError("Unsafe extracted image path")
            asset = workspace.root / relative
            try:
                resolved = asset.resolve(strict=True)
                if not resolved.is_relative_to(root) or asset.is_symlink() or not resolved.is_file():
                    raise OSError("Image asset is outside the job workspace")
                if image.media_type not in {"image/jpeg", "image/png"}:
                    raise EpubImageEmbeddingError("An extracted PDF image has an unsupported format")
                expected_format = "JPEG" if image.media_type == "image/jpeg" else "PNG"
                with Image.open(resolved) as decoded:
                    if decoded.format != expected_format:
                        raise EpubImageEmbeddingError("An extracted PDF image format does not match its metadata")
                    width, height = decoded.size
                    if (
                        (width, height) != (image.width, image.height)
                        or width < 1 or height < 1
                        or width * height > MAX_IMAGE_PIXELS
                        or max(width, height) > MAX_IMAGE_DIMENSION
                    ):
                        raise EpubImageEmbeddingError("An extracted PDF image has invalid or excessive dimensions")
                    decoded.verify()
                with Image.open(resolved) as decoded:
                    decoded.load()
            except EpubImageEmbeddingError:
                raise
            except Exception as exc:
                raise EpubImageEmbeddingError(
                    "An extracted PDF image is missing, unsafe, or undecodable"
                ) from exc
            assets.append((image, resolved))
    return assets


def _optimized_image_bytes(asset_path: Path, media_type: str) -> bytes:
    """Re-encode extracted images conservatively before embedding them in the EPUB."""
    original = asset_path.read_bytes()
    output = io.BytesIO()
    try:
        with Image.open(asset_path) as decoded:
            decoded.load()
            if media_type == "image/jpeg":
                # Keep grayscale assets grayscale; normalize other JPEG modes to RGB.
                prepared = decoded.copy() if decoded.mode == "L" else decoded.convert("RGB")
                try:
                    prepared.save(
                        output,
                        format="JPEG",
                        quality=85,
                        optimize=True,
                        progressive=True,
                        icc_profile=(
                            decoded.info.get("icc_profile")
                            if decoded.mode == "RGB" else None
                        ),
                    )
                finally:
                    prepared.close()
            elif media_type == "image/png":
                # PNG remains lossless and retains alpha/transparency.
                decoded.save(output, format="PNG", optimize=True)
            else:
                return original
    except (OSError, ValueError):
        # The asset was already validated by _load_assets. If a codec still cannot
        # re-encode it, preserve the original bytes rather than failing the book.
        return original

    optimized = output.getvalue()
    # Avoid generation loss when a lossy JPEG re-encode does not materially shrink it.
    return optimized if len(optimized) <= len(original) * 0.9 else original


def has_current_image_layout(epub_path: Path | str) -> bool:
    """Return whether an EPUB was built with the source-anchor image layout."""
    try:
        with zipfile.ZipFile(epub_path, "r") as archive:
            return any(
                name.startswith("OEBPS/chapter_") and name.endswith(".xhtml")
                and b"inkslim-image-layout:5" in archive.read(name)
                for name in archive.namelist()
            )
    except (OSError, zipfile.BadZipFile, KeyError):
        return False


def embedded_image_count(epub_path: Path | str) -> int:
    """Count image resources in the EPUB's OEBPS/images directory."""
    try:
        with zipfile.ZipFile(epub_path, "r") as archive:
            return sum(
                name.startswith("OEBPS/images/")
                and name.lower().endswith((".jpg", ".jpeg", ".png"))
                for name in archive.namelist()
            )
    except (OSError, zipfile.BadZipFile):
        return 0


def _paragraph_records(chapter_bytes: bytes) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for match in _PARAGRAPH_RE.finditer(chapter_bytes):
        attributes = match.group(2)
        page_match = _SOURCE_PAGE_RE.search(attributes)
        bbox_match = _SOURCE_BBOX_RE.search(attributes)
        id_match = _ID_RE.search(attributes)
        if not (page_match and bbox_match and id_match):
            continue
        try:
            page_number = int(page_match.group(1))
            bbox = tuple(float(value) for value in bbox_match.group(1).decode("ascii").split(","))
        except (ValueError, UnicodeDecodeError):
            continue
        if len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
            continue
        records.append({
            "page": page_number,
            "bbox": bbox,
            "id": id_match.group(1).decode("ascii"),
        })
    return records


def _select_anchor(
    image: PageImage, records: list[dict[str, object]]
) -> str | None:
    """Anchor only when the image fits wholly in a clear vertical text gap."""
    image_x0, image_top, image_x1, image_bottom = image.bbox
    image_width = image_x1 - image_x0
    if image_width <= 0:
        return None

    page_records = []
    for record in records:
        if record["page"] != image.page_number:
            continue
        x0, _y0, x1, _y1 = record["bbox"]
        overlap = max(0.0, min(x1, image_x1) - max(x0, image_x0))
        narrower_width = min(image_width, x1 - x0)
        if narrower_width > 0 and overlap / narrower_width >= 0.25:
            page_records.append(record)
    if not page_records:
        return None
    preceding = [
        record for record in page_records
        if record["bbox"][3] <= image_top + 0.002
    ]
    following = [
        record for record in page_records
        if record["bbox"][1] >= image_bottom - 0.002
    ]
    if not preceding:
        return None

    previous = max(preceding, key=lambda record: record["bbox"][3])
    next_record = min(following, key=lambda record: record["bbox"][1]) if following else None
    if next_record is None:
        return None

    # Do not anchor when the image overlaps any text paragraph vertically.
    for record in page_records:
        if record is previous or record is next_record:
            continue
        top, bottom = record["bbox"][1], record["bbox"][3]
        if max(top, image_top) < min(bottom, image_bottom) - 0.002:
            return None

    if image_top < previous["bbox"][3] - 0.002:
        return None
    if image_bottom > next_record["bbox"][1] + 0.002:
        return None

    previous_center = (previous["bbox"][0] + previous["bbox"][2]) / 2
    next_center = (next_record["bbox"][0] + next_record["bbox"][2]) / 2
    if abs(previous_center - next_center) > 0.18:
        return None
    return str(previous["id"])


def _figure_markup(image: PageImage, *, cover: bool = False) -> bytes:
    href = image.source_path
    if cover:
        return (
            '<figure class="pdf-cover">'
            f'<img src={quoteattr(href)} alt="Book cover"/>'
            '</figure>'
        ).encode("utf-8")
    alt = f"Image from source PDF page {image.page_number}"
    return (
        '<figure class="pdf-illustration">'
        f'<img src={quoteattr(href)} alt={quoteattr(alt)}/>'
        f'<figcaption>Source PDF page {image.page_number}</figcaption>'
        '</figure>'
    ).encode("utf-8")


def _select_page_start_anchor(
    image: PageImage, records: list[dict[str, object]]
) -> str | None:
    """Place a page-sized image before the first text block on its source page."""
    candidates = [
        record for record in records
        if record["page"] >= image.page_number
    ]
    if not candidates:
        return None
    same_page = [record for record in candidates if record["page"] == image.page_number]
    selected = same_page[0] if same_page else candidates[0]
    return str(selected["id"])


def embed_pdf_images(
    epub_path: Path | str,
    workspace: JobWorkspace,
    *,
    total_pages: int,
    page_classifications: Mapping[int, Mapping[str, object]] | None = None,
) -> int:
    """Embed source images in reading order and mark a confident cover."""
    assets = _load_assets(workspace, total_pages)
    if not assets:
        return 0

    # Classification is advisory. Only a strong first-page cover candidate is
    # promoted to EPUB cover metadata; no extracted asset is discarded.
    cover_asset: tuple[PageImage, Path] | None = None
    if page_classifications:
        first_page = page_classifications.get(1, {})
        if (
            first_page.get("type") == "cover_candidate"
            and float(first_page.get("confidence", 0.0)) >= 0.75
        ):
            candidates = [
                asset for asset in assets
                if asset[0].page_number == 1 and asset[0].coverage >= 0.45
            ]
            if candidates:
                cover_asset = max(candidates, key=lambda asset: asset[0].coverage)

    epub_path = Path(epub_path)
    handle, temporary_name = tempfile.mkstemp(
        dir=epub_path.parent, prefix=f".{epub_path.name}.", suffix=".tmp"
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(epub_path, "r") as source:
            infos = source.infolist()
            info_by_name = {info.filename: info for info in infos}
            if "OEBPS/content.opf" not in info_by_name:
                raise EpubImageEmbeddingError("Generated EPUB has no package document")

            opf_root = ET.fromstring(source.read(info_by_name["OEBPS/content.opf"]))
            manifest = opf_root.find(f"{{{_OPF_NS}}}manifest")
            spine = opf_root.find(f"{{{_OPF_NS}}}spine")
            if manifest is None or spine is None:
                raise EpubImageEmbeddingError("Generated EPUB has an invalid package manifest or spine")

            chapter_paths = [
                "OEBPS/" + item.get("href", "")
                for itemref in spine.findall(f"{{{_OPF_NS}}}itemref")
                if (item := next(
                    (entry for entry in manifest.findall(f"{{{_OPF_NS}}}item")
                     if entry.get("id") == itemref.get("idref")),
                    None,
                )) is not None
                and item.get("media-type") == "application/xhtml+xml"
                and "OEBPS/" + item.get("href", "") in info_by_name
            ]
            if not chapter_paths:
                raise EpubImageEmbeddingError("Generated EPUB has no chapter documents")
            first_chapter = chapter_paths[0]
            last_chapter = chapter_paths[-1]

            for index, (image, _asset_path) in enumerate(assets, start=1):
                item_id = f"pdf-image-{index:04d}"
                attributes = {
                    "id": item_id,
                    "href": image.source_path,
                    "media-type": image.media_type,
                }
                if cover_asset is not None and image == cover_asset[0]:
                    attributes["properties"] = "cover-image"
                manifest.append(ET.Element(f"{{{_OPF_NS}}}item", attributes))
            opf_bytes = ET.tostring(opf_root, encoding="utf-8", xml_declaration=True)

            css_info = info_by_name.get("OEBPS/styles.css")
            css_data = None
            if css_info is not None:
                css_data = source.read(css_info) + (
                    b"\n.pdf-illustration, .pdf-cover { margin: 1.5em 0; text-align: center; }"
                    b"\n.pdf-illustration img, .pdf-cover img { max-width: 100%; height: auto; }"
                    b"\n.pdf-illustration figcaption { font-size: 0.85em; text-align: center; }\n"
                )

            # Keep only one uncompressed chapter in memory at a time. Images are
            # assigned as the spine is streamed, and unresolved assets flow into
            # the final chapter's appendix.
            # Every source image stays in source-page order. Page classification
            # must not move source content to the end of the book.
            pending = [
                asset for asset in assets
                if cover_asset is None or asset != cover_asset
            ]

            with zipfile.ZipFile(temporary, "w") as target:
                first = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
                first.compress_type = zipfile.ZIP_STORED
                first.external_attr = 0o644 << 16
                target.writestr(first, source.read("mimetype"))

                chapter_path_set = set(chapter_paths)
                for info in infos:
                    name = info.filename
                    if name == "mimetype" or name in chapter_path_set:
                        continue
                    if name == "OEBPS/content.opf":
                        target.writestr(info, opf_bytes)
                    elif name == "OEBPS/styles.css" and css_data is not None:
                        target.writestr(info, css_data)
                    else:
                        with source.open(info, "r") as input_stream, target.open(
                            info, "w"
                        ) as output_stream:
                            shutil.copyfileobj(
                                input_stream, output_stream, length=1024 * 1024
                            )

                # Process chapters in spine order, independent of ZIP member order.
                for chapter_path in chapter_paths:
                    info = info_by_name[chapter_path]
                    chapter_bytes = source.read(info)
                    records = _paragraph_records(chapter_bytes)
                    anchored: dict[str, list[tuple[PageImage, bool]]] = {}
                    remaining: list[tuple[PageImage, Path]] = []

                    for image, asset_path in pending:
                        anchor = _select_anchor(image, records)
                        insert_before = False
                        # Large page images often occupy the whole page and cannot fit
                        # in a text gap. Keep them at the beginning of their source page.
                        if anchor is None and image.coverage >= 0.45:
                            anchor = _select_page_start_anchor(image, records)
                            insert_before = anchor is not None
                        if anchor is None:
                            remaining.append((image, asset_path))
                        else:
                            anchored.setdefault(anchor, []).append((image, insert_before))
                    pending = remaining

                    for anchor_id, placements in anchored.items():
                        escaped_id = re.escape(anchor_id.encode("ascii"))
                        pattern = re.compile(
                            rb'(<(?:p|h2|h3)\b(?=[^>]*\bid="' + escaped_id
                            + rb'")[^>]*>.*?</(?:p|h2|h3)>)',
                            re.DOTALL,
                        )
                        before_markup = b"\n".join(
                            _figure_markup(image)
                            for image, insert_before in placements if insert_before
                        )
                        after_markup = b"\n".join(
                            _figure_markup(image)
                            for image, insert_before in placements if not insert_before
                        )
                        chapter_bytes, count = pattern.subn(
                            lambda match: (
                                before_markup + b"\n" if before_markup else b""
                            ) + match.group(1) + (
                                b"\n" + after_markup if after_markup else b""
                            ),
                            chapter_bytes,
                            count=1,
                        )
                        if count != 1:
                            raise EpubImageEmbeddingError(
                                "Could not locate an image's source text block in the EPUB"
                            )

                    if chapter_path == first_chapter:
                        if cover_asset is not None:
                            cover_markup = b"\n" + _figure_markup(cover_asset[0], cover=True)
                            chapter_bytes, count = re.subn(
                                rb"(<section\\b[^>]*>)",
                                lambda match: match.group(1) + cover_markup,
                                chapter_bytes,
                                count=1,
                            )
                            if count != 1:
                                raise EpubImageEmbeddingError(
                                    "Could not place the cover at the start of the first EPUB chapter"
                                )
                        if _XHTML_END not in chapter_bytes:
                            raise EpubImageEmbeddingError(
                                "First EPUB chapter has no content section"
                            )
                        chapter_bytes = chapter_bytes.replace(
                            _XHTML_END,
                            b"<!-- inkslim-image-layout:5 -->\n</section>",
                            1,
                        )

                    if chapter_path == last_chapter and pending:
                        if _XHTML_END not in chapter_bytes:
                            raise EpubImageEmbeddingError(
                                "Final EPUB chapter has no content section"
                            )
                        appendix_markup = (
                            b"\n<h2>Illustrations</h2>\n"
                            + b"\n".join(
                                _figure_markup(image) for image, _asset_path in pending
                            )
                            + b"\n"
                        )
                        chapter_bytes = chapter_bytes.replace(
                            _XHTML_END, appendix_markup + _XHTML_END, 1
                        )
                        pending = []

                    target.writestr(info, chapter_bytes)

                if pending:
                    raise EpubImageEmbeddingError(
                        "Some extracted images could not be assigned to an EPUB chapter"
                    )

                for image, asset_path in assets:
                    optimized_bytes = _optimized_image_bytes(asset_path, image.media_type)
                    with target.open(
                        "OEBPS/" + image.source_path, "w", force_zip64=True
                    ) as archive_stream:
                        archive_stream.write(optimized_bytes)

        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, epub_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return len(assets)
