from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from conversion.epub.builder import Chapter, Block, EpubMetadata, build_epub
from conversion.epub.generator import is_valid_epub
from conversion.epub.image_embedding import embed_pdf_images, embedded_image_count
from conversion.pdf.images import PageImage, PageImages
from conversion.storage import JobWorkspace

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "x": "http://www.w3.org/1999/xhtml",
}


def test_embeds_extracted_images_and_adds_manifest_and_reading_order_markup(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    image_path = workspace.page_image_asset_prefix(1, 1).with_suffix(".png")
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), "red").save(image_path, "PNG")
    workspace.save_page_images(PageImages(
        page_number=1,
        images=(PageImage(1, 1, (0.1, 0.1, 0.9, 0.9), 80, 60,
                          "image/png", "images/000001-0001.png", 0.64),),
        warnings=(),
        detected_count=1,
    ))
    # Keep the fixture's path consistent with the checkpoint.
    (workspace.root / "images" / "000001-0001.png").write_bytes(image_path.read_bytes())

    epub = build_epub(
        tmp_path / "book.epub",
        EpubMetadata(title="Test book"),
        [Chapter("Chapter 1", [Block("p", "Readable text.")])],
    )
    assert embed_pdf_images(epub, workspace, total_pages=1) == 1
    assert embedded_image_count(epub) == 1
    assert is_valid_epub(epub)

    with zipfile.ZipFile(epub) as archive:
        assert "OEBPS/images/000001-0001.png" in archive.namelist()
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        image_items = opf.findall("opf:manifest/opf:item[@media-type='image/png']", NS)
        assert len(image_items) == 1
        chapter = ET.fromstring(archive.read("OEBPS/chapter_0001.xhtml"))
        image = chapter.find(".//x:img", NS)
        assert image is not None
        assert image.get("alt") == "Image from source PDF page 1"
        assert image.get("src") == "images/000001-0001.png"


def test_no_images_leaves_epub_unchanged(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    epub = build_epub(
        tmp_path / "book.epub",
        EpubMetadata(title="Text only"),
        [Chapter("Chapter 1", [Block("p", "Readable text.")])],
    )
    before = epub.read_bytes()
    assert embed_pdf_images(epub, workspace, total_pages=1) == 0
    assert epub.read_bytes() == before
