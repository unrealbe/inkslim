"""Embed extracted PDF image assets into a generated EPUB.

Images are currently collected in a clearly labelled source-page appendix. This keeps
all extracted artwork available without guessing at semantic reading order; inline
placement and cover deduplication are handled by a later page-classification milestone.
"""
from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from ..storage import JobWorkspace

_OPF_NS = "http://www.idpf.org/2007/opf"
_XHTML_END = b"</section>"
ET.register_namespace("", _OPF_NS)
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")


class EpubImageEmbeddingError(ValueError):
    """Raised when extracted image assets cannot safely be embedded."""


def _load_assets(workspace: JobWorkspace, total_pages: int) -> list[tuple[int, str, str, bytes]]:
    root = workspace.root.resolve()
    assets: list[tuple[int, str, str, bytes]] = []
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
                data = resolved.read_bytes()
            except (OSError, RuntimeError) as exc:
                raise EpubImageEmbeddingError("An extracted PDF image asset is missing or unsafe") from exc

            if image.media_type == "image/jpeg" and data.startswith(b"\\xff\\xd8\\xff"):
                extension = "jpg"
            elif image.media_type == "image/png" and data.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
                extension = "png"
            else:
                raise EpubImageEmbeddingError("An extracted PDF image has an unsupported or invalid format")
            assets.append((page_number, f"images/pdf-page-{page_number:06d}-{image.image_index:04d}.{extension}",
                           image.media_type, data))
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
    with zipfile.ZipFile(epub_path, "r") as source:
        entries = {info.filename: (info, source.read(info.filename)) for info in source.infolist()}
    if "OEBPS/content.opf" not in entries:
        raise EpubImageEmbeddingError("Generated EPUB has no package document")

    opf_root = ET.fromstring(entries["OEBPS/content.opf"][1])
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
    if chapter_path not in entries:
        raise EpubImageEmbeddingError("Final EPUB chapter is missing")

    chapter_bytes = entries[chapter_path][1]
    if _XHTML_END not in chapter_bytes:
        raise EpubImageEmbeddingError("Final EPUB chapter has no content section")

    figure_markup: list[str] = ['<h2>Illustrations</h2>']
    image_entries: list[tuple[str, bytes]] = []
    for index, (page_number, href, media_type, data) in enumerate(assets, start=1):
        image_id = f"pdf-image-{index:04d}"
        manifest.append(ET.Element(f"{{{_OPF_NS}}}item", {
            "id": image_id,
            "href": href,
            "media-type": media_type,
        }))
        image_entries.append(("OEBPS/" + href, data))
        alt = f"Image from source PDF page {page_number}"
        figure_markup.append(
            '<figure class="pdf-illustration">'
            f'<img src={quoteattr(href)} alt={quoteattr(alt)}/>'
            f'<figcaption>Source PDF page {page_number}</figcaption>'
            '</figure>'
        )

    insertion = ("\\n" + "\\n".join(figure_markup) + "\\n").encode("utf-8")
    chapter_bytes = chapter_bytes.replace(_XHTML_END, insertion + _XHTML_END, 1)
    entries[chapter_path] = (entries[chapter_path][0], chapter_bytes)

    opf_bytes = ET.tostring(opf_root, encoding="utf-8", xml_declaration=True)
    entries["OEBPS/content.opf"] = (entries["OEBPS/content.opf"][0], opf_bytes)

    css_info, css_data = entries.get("OEBPS/styles.css", (None, b""))
    if css_info is not None:
        css_data += b"\\n.pdf-illustration { margin: 1.5em 0; text-align: center; }\\n.pdf-illustration img { max-width: 100%; height: auto; }\\n.pdf-illustration figcaption { font-size: 0.85em; text-align: center; }\\n"
        entries["OEBPS/styles.css"] = (css_info, css_data)

    handle, temporary_name = tempfile.mkstemp(
        dir=epub_path.parent, prefix=f".{epub_path.name}.", suffix=".tmp"
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w") as target:
            first = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
            first.compress_type = zipfile.ZIP_STORED
            first.external_attr = 0o644 << 16
            target.writestr(first, entries["mimetype"][1])
            for name, (info, data) in entries.items():
                if name == "mimetype":
                    continue
                target.writestr(name, data, compress_type=info.compress_type)
            for name, data in image_entries:
                target.writestr(name, data, compress_type=zipfile.ZIP_STORED)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, epub_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return len(assets)
