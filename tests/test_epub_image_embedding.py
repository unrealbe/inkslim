from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

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


def _save_image(workspace, page_number, image_index, colour):
    path = workspace.page_image_asset_prefix(page_number, image_index).with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), colour).save(path, "PNG")
    relative_path = path.relative_to(workspace.root).as_posix()
    workspace.save_page_images(PageImages(
        page_number=page_number,
        images=(PageImage(
            page_number, image_index, (0.1, 0.1, 0.9, 0.9), 80, 60,
            "image/png", relative_path, 0.64,
        ),),
        warnings=(),
        detected_count=1,
    ))
    return relative_path


def test_embeds_extracted_images_in_source_page_order_with_manifest_and_markup(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()

    # Save page 2 first to ensure output order comes from source page numbers,
    # not checkpoint creation order.
    page_two_path = _save_image(workspace, 2, 1, "blue")
    page_one_path = _save_image(workspace, 1, 1, "red")

    epub = build_epub(
        tmp_path / "book.epub",
        EpubMetadata(title="Test book"),
        [Chapter("Chapter 1", [Block("p", "Readable text.")])],
    )
    assert embed_pdf_images(epub, workspace, total_pages=2) == 2
    assert embedded_image_count(epub) == 2
    assert is_valid_epub(epub)

    with zipfile.ZipFile(epub) as archive:
        assert f"OEBPS/{page_one_path}" in archive.namelist()
        assert f"OEBPS/{page_two_path}" in archive.namelist()
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        image_items = opf.findall("opf:manifest/opf:item[@media-type='image/png']", NS)
        assert len(image_items) == 2

        chapter = ET.fromstring(archive.read("OEBPS/chapter_0001.xhtml"))
        figures = chapter.findall(".//x:figure", NS)
        assert len(figures) == 2
        captions = [figure.find("x:figcaption", NS).text for figure in figures]
        assert captions == ["Source PDF page 1", "Source PDF page 2"]
        image_sources = [figure.find("x:img", NS).get("src") for figure in figures]
        assert image_sources == [page_one_path, page_two_path]
        image_alts = [figure.find("x:img", NS).get("alt") for figure in figures]
        assert image_alts == [
            "Image from source PDF page 1",
            "Image from source PDF page 2",
        ]


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



def test_embedding_oserror_removes_text_only_epub(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from conversion.epub import generator
    from conversion.worker import JobProcessingError

    workspace = SimpleNamespace(
        output_dir=tmp_path,
        load_page_images=lambda _page: None,
        load_page_cleaned_paragraphs=lambda _page: [],
        load_structure_analysis=lambda: {},
        load_page_structure=lambda _page: [],
        save_document_model=lambda _doc: None,
    )
    job = SimpleNamespace(
        id="12345678-1234-5678-1234-567812345678",
        input_name="book.pdf",
        language="auto",
    )
    document = SimpleNamespace(
        metadata=SimpleNamespace(identifier=None),
        to_epub_chapters=lambda: [],
    )

    monkeypatch.setattr(generator, "is_valid_epub", lambda _path: False)
    monkeypatch.setattr(generator, "build_document_model", lambda *args, **kwargs: document)

    def write_text_only_epub(destination, **_kwargs):
        destination.write_bytes(b"text-only epub")

    def fail_embedding(*_args, **_kwargs):
        raise OSError("simulated image archive write failure")

    monkeypatch.setattr(generator, "build_epub", write_text_only_epub)
    monkeypatch.setattr(generator, "embed_pdf_images", fail_embedding)

    with pytest.raises(JobProcessingError) as exc_info:
        generator.generate_job_epub(job, workspace, total_pages=1)

    destination = tmp_path / "book-12345678.epub"
    assert exc_info.value.error_code == "STORAGE_ERROR"
    assert not destination.exists()


def test_corrupt_image_asset_is_rejected_before_embedding(tmp_path):
    import pytest

    from conversion.epub.image_embedding import EpubImageEmbeddingError

    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    path = workspace.page_image_asset_prefix(1, 1).with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Valid PNG signature, but truncated before a decodable image payload.
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR")
    relative_path = path.relative_to(workspace.root).as_posix()
    workspace.save_page_images(PageImages(
        page_number=1,
        images=(PageImage(
            1, 1, (0.1, 0.1, 0.9, 0.9), 80, 60,
            "image/png", relative_path, 0.64,
        ),),
        warnings=(),
        detected_count=1,
    ))
    epub = build_epub(
        tmp_path / "book.epub",
        EpubMetadata(title="Corrupt image"),
        [Chapter("Chapter 1", [Block("p", "Readable text.")])],
    )
    before = epub.read_bytes()
    with pytest.raises(EpubImageEmbeddingError, match="undecodable"):
        embed_pdf_images(epub, workspace, total_pages=1)
    assert epub.read_bytes() == before


def test_image_dimensions_must_match_checkpoint_and_limits(tmp_path):
    import pytest

    from conversion.epub.image_embedding import EpubImageEmbeddingError

    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    path = workspace.page_image_asset_prefix(1, 1).with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    # The file is valid, but its real dimensions disagree with the checkpoint.
    Image.new("RGB", (81, 60), "green").save(path, "PNG")
    relative_path = path.relative_to(workspace.root).as_posix()
    workspace.save_page_images(PageImages(
        page_number=1,
        images=(PageImage(
            1, 1, (0.1, 0.1, 0.9, 0.9), 80, 60,
            "image/png", relative_path, 0.64,
        ),),
        warnings=(),
        detected_count=1,
    ))
    epub = build_epub(
        tmp_path / "book.epub",
        EpubMetadata(title="Mismatched image dimensions"),
        [Chapter("Chapter 1", [Block("p", "Readable text.")])],
    )
    with pytest.raises(EpubImageEmbeddingError, match="dimensions"):
        embed_pdf_images(epub, workspace, total_pages=1)


@pytest.mark.parametrize("width,height", [(12001, 1), (7100, 7100)])
def test_matching_checkpoint_image_over_limits_is_rejected(tmp_path, width, height):
    import binascii
    import struct

    import pytest

    from conversion.epub.image_embedding import EpubImageEmbeddingError

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        )

    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    path = workspace.page_image_asset_prefix(1, 1).with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    # A tiny PNG container with oversized IHDR dimensions. The dimensions
    # match the checkpoint, so the configured dimension/pixel guards are tested.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\\x89PNG\\r\\n\\x1a\\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", b"")
        + chunk(b"IEND", b"")
    )
    relative_path = path.relative_to(workspace.root).as_posix()
    workspace.save_page_images(PageImages(
        page_number=1,
        images=(PageImage(
            1, 1, (0.1, 0.1, 0.9, 0.9), width, height,
            "image/png", relative_path, 0.64,
        ),),
        warnings=(),
        detected_count=1,
    ))
    epub = build_epub(
        tmp_path / "book.epub",
        EpubMetadata(title="Oversized image"),
        [Chapter("Chapter 1", [Block("p", "Readable text.")])],
    )
    with pytest.raises(EpubImageEmbeddingError, match="dimensions"):
        embed_pdf_images(epub, workspace, total_pages=1)
