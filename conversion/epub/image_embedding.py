"""Embed extracted PDF image assets into a generated EPUB.

Images are currently collected in a clearly labelled source-page appendix. This keeps
all extracted artwork available without guessing at semantic reading order; inline
placement and cover deduplication are handled by a later page-classification milestone.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from PIL import Image

from ..pdf.images import MAX_IMAGE_DIMENSION, MAX_IMAGE_PIXELS

from ..storage import JobWorkspace

_OPF_NS = "http://www.idpf.org/2007/opf"
_XHTML_END = b"</section>"
ET.register_namespace("", _OPF_NS)
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")


class EpubImageEmbeddingError(ValueError):
    """Raised when extracted image assets cannot safely be embedded."""


def _load_assets(workspace: JobWorkspace, total_pages: int) -> list[tuple[int, str, str, Path]]:
    root = workspace.root.resolve()
    assets: list[tuple[int, str, str, Path]] = []
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
                # Reopen and force full pixel decoding; verify() alone is not sufficient.
                with Image.open(resolved) as decoded:
                    decoded.load()
            except EpubImageEmbeddingError:
                raise
            except Exception as exc:
                # Pillow may raise EOFError, struct/zlib errors or decoder-specific
                # exceptions for damaged payloads; normalize all decode failures.
                raise EpubImageEmbeddingError("An extracted PDF image is missing, unsafe, or undecodable") from exc

            assets.append((page_number, image.source_path, image.media_type, resolved))
    return assets


def embedded_image_count(epub_path: Path | str) -> int:
    """Count image resources in the EPUB's OEBPS/images directory."""
    try:
        with zipfile.ZipFile(epub_path, "r") as archive:
            return sum(
                name.startswith("OEBPS/images/") and name.lower().endswith((".jpg", ".jpeg", ".png"))
                for name in archive.namelist()
            )
    except (OSError, zipfile.BadZipFile):
        return 0


def embed_pdf_images(epub_path: Path | str, workspace: JobWorkspace, *, total_pages: int) -> int:
    """Add extracted page images to the EPUB and return the number embedded."""
    assets = _load_assets(workspace, total_pages)
    if not assets:
        return 0

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

            opf_info = info_by_name["OEBPS/content.opf"]
            opf_root = ET.fromstring(source.read(opf_info))
            manifest = opf_root.find(f"{{{_OPF_NS}}}manifest")
            spine = opf_root.find(f"{{{_OPF_NS}}}spine")
            if manifest is None or spine is None:
                raise EpubImageEmbeddingError("Generated EPUB has an invalid package manifest or spine")

            item_by_id = {item.get("id"): item for item in manifest.findall(f"{{{_OPF_NS}}}item")}
            spine_refs = spine.findall(f"{{{_OPF_NS}}}itemref")
            last_ref = spine_refs[-1].get("idref") if spine_refs else None
            last_item = item_by_id.get(last_ref)
            if last_item is None:
                raise EpubImageEmbeddingError("Generated EPUB has no final reading-order document")
            chapter_path = "OEBPS/" + last_item.get("href", "")
            chapter_info = info_by_name.get(chapter_path)
            if chapter_info is None:
                raise EpubImageEmbeddingError("Final EPUB chapter is missing")

            chapter_bytes = source.read(chapter_info)
            if _XHTML_END not in chapter_bytes:
                raise EpubImageEmbeddingError("Final EPUB chapter has no content section")

            figure_markup: list[str] = ['<h2>Illustrations</h2>']
            image_entries: list[tuple[str, Path]] = []
            for index, (page_number, href, media_type, asset_path) in enumerate(assets, start=1):
                image_id = f"pdf-image-{index:04d}"
                manifest.append(ET.Element(f"{{{_OPF_NS}}}item", {
                    "id": image_id,
                    "href": href,
                    "media-type": media_type,
                }))
                image_entries.append(("OEBPS/" + href, asset_path))
                alt = f"Image from source PDF page {page_number}"
                figure_markup.append(
                    '<figure class="pdf-illustration">'
                    f'<img src={quoteattr(href)} alt={quoteattr(alt)}/>'
                    f'<figcaption>Source PDF page {page_number}</figcaption>'
                    '</figure>'
                )

            insertion = ("\\n" + "\\n".join(figure_markup) + "\\n").encode("utf-8")
            chapter_bytes = chapter_bytes.replace(_XHTML_END, insertion + _XHTML_END, 1)
            opf_bytes = ET.tostring(opf_root, encoding="utf-8", xml_declaration=True)

            css_info = info_by_name.get("OEBPS/styles.css")
            css_data = None
            if css_info is not None:
                css_data = source.read(css_info) + (
                    b"\\n.pdf-illustration { margin: 1.5em 0; text-align: center; }"
                    b"\\n.pdf-illustration img { max-width: 100%; height: auto; }"
                    b"\\n.pdf-illustration figcaption { font-size: 0.85em; text-align: center; }\\n"
                )

            with zipfile.ZipFile(temporary, "w") as target:
                first = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
                first.compress_type = zipfile.ZIP_STORED
                first.external_attr = 0o644 << 16
                target.writestr(first, source.read("mimetype"))

                for info in infos:
                    name = info.filename
                    if name == "mimetype":
                        continue
                    if name == "OEBPS/content.opf":
                        target.writestr(info, opf_bytes)
                    elif name == chapter_path:
                        target.writestr(info, chapter_bytes)
                    elif name == "OEBPS/styles.css" and css_data is not None:
                        target.writestr(info, css_data)
                    else:
                        # Copy unchanged members incrementally; do not retain the
                        # whole EPUB's uncompressed payloads in worker memory.
                        with source.open(info, "r") as input_stream, target.open(
                            info, "w", force_zip64=True
                        ) as output_stream:
                            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)

                for name, asset_path in image_entries:
                    with asset_path.open("rb") as image_stream, target.open(
                        name, "w", force_zip64=True
                    ) as archive_stream:
                        shutil.copyfileobj(image_stream, archive_stream, length=1024 * 1024)

        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, epub_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return len(assets)
