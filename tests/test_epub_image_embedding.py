from __future__ import annotations

import pytest
import zipfile
import xml.etree.ElementTree as ET

from PIL import Image

from conversion.epub.builder import Chapter, Block, EpubMetadata, build_epub
from conversion.epub.generator import is_valid_epub
from conversion.epub.image_embedding import (
    embed_pdf_images,
    embedded_image_count,
    has_current_image_layout,
)
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


def test_jpeg_assets_are_reencoded_when_materially_smaller(tmp_path):
    import io

    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    path = workspace.page_image_asset_prefix(1, 1).with_suffix(".jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.effect_noise((800, 1200), 90).convert("RGB").save(
        path, "JPEG", quality=100, subsampling=0
    )
    original_size = path.stat().st_size
    relative_path = path.relative_to(workspace.root).as_posix()
    workspace.save_page_images(PageImages(
        page_number=1,
        images=(PageImage(
            1, 1, (0.1, 0.1, 0.9, 0.9), 800, 1200,
            "image/jpeg", relative_path, 0.64,
        ),),
        warnings=(),
        detected_count=1,
    ))
    epub = build_epub(
        tmp_path / "compressed.epub",
        EpubMetadata(title="JPEG optimization"),
        [Chapter("Chapter 1", [Block("p", "Readable text.")])],
    )

    assert embed_pdf_images(epub, workspace, total_pages=1) == 1
    with zipfile.ZipFile(epub) as archive:
        optimized = archive.read("OEBPS/" + relative_path)
    assert len(optimized) < original_size * 0.9
    with Image.open(io.BytesIO(optimized)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (800, 1200)


def test_png_optimization_preserves_alpha(tmp_path):
    import io

    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    path = workspace.page_image_asset_prefix(1, 1).with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (80, 60), (255, 0, 0, 100)).save(path, "PNG")
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
        tmp_path / "transparent.epub",
        EpubMetadata(title="PNG alpha"),
        [Chapter("Chapter 1", [Block("p", "Readable text.")])],
    )
    embed_pdf_images(epub, workspace, total_pages=1)
    with zipfile.ZipFile(epub) as archive:
        optimized = archive.read("OEBPS/" + relative_path)
    with Image.open(io.BytesIO(optimized)) as decoded:
        assert decoded.mode == "RGBA"
        assert decoded.getpixel((0, 0))[3] == 100


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
        save_document_language_analysis=lambda _analysis: None,
    )
    job = SimpleNamespace(
        id="12345678-1234-5678-1234-567812345678",
        input_name="book.pdf",
        language="eng",
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
        b"\x89PNG\r\n\x1a\n"
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



def test_places_image_between_paragraphs_on_its_source_page(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    path = workspace.page_image_asset_prefix(1, 1).with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), "orange").save(path, "PNG")
    relative_path = path.relative_to(workspace.root).as_posix()
    workspace.save_page_images(PageImages(
        page_number=1,
        images=(PageImage(
            1, 1, (0.15, 0.30, 0.85, 0.55), 80, 60,
            "image/png", relative_path, 0.175,
        ),),
        warnings=(),
        detected_count=1,
    ))

    epub = build_epub(
        tmp_path / "inline.epub",
        EpubMetadata(title="Inline placement"),
        [Chapter("Chapter 1", [
            Block("p", "Text before the illustration.", 1, 1, (0.1, 0.1, 0.9, 0.2)),
            Block("p", "Text after the illustration.", 1, 2, (0.1, 0.7, 0.9, 0.8)),
        ])],
    )
    assert embed_pdf_images(epub, workspace, total_pages=1) == 1
    assert has_current_image_layout(epub)

    with zipfile.ZipFile(epub) as archive:
        chapter = archive.read("OEBPS/chapter_0001.xhtml")
        before = chapter.index(b"Text before the illustration.")
        figure = chapter.index(b'<figure class="pdf-illustration">')
        after = chapter.index(b"Text after the illustration.")
        assert before < figure < after
        assert b"<h2>Illustrations</h2>" not in chapter
        assert b"data-source-page=\"1\"" in chapter
        assert b"<!-- inkslim-image-layout:4 -->\n</section>" in chapter
        assert b"<!-- inkslim-image-layout:4 -->\\n</section>" not in chapter
        assert f"OEBPS/{relative_path}" in archive.namelist()


def test_image_without_bounded_text_gap_stays_in_appendix(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    path = workspace.page_image_asset_prefix(1, 1).with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), "purple").save(path, "PNG")
    relative_path = path.relative_to(workspace.root).as_posix()
    workspace.save_page_images(PageImages(
        page_number=1,
        images=(PageImage(
            1, 1, (0.15, 0.30, 0.85, 0.55), 80, 60,
            "image/png", relative_path, 0.175,
        ),),
        warnings=(),
        detected_count=1,
    ))
    epub = build_epub(
        tmp_path / "fallback.epub",
        EpubMetadata(title="Safe fallback"),
        [Chapter("Chapter 1", [
            Block("p", "Only paragraph.", 1, 1, (0.1, 0.1, 0.9, 0.2)),
        ])],
    )
    embed_pdf_images(epub, workspace, total_pages=1)
    with zipfile.ZipFile(epub) as archive:
        chapter = archive.read("OEBPS/chapter_0001.xhtml")
        appendix = chapter.index(b"<h2>Illustrations</h2>")
        figure = chapter.index(b'<figure class="pdf-illustration">', appendix)
        image_src = chapter.index(f'src="{relative_path}"'.encode("utf-8"), figure)
        assert chapter.index(b"Only paragraph.") < appendix < figure < image_src



def test_negative_paragraph_index_does_not_emit_duplicate_source_anchor_ids(tmp_path):
    epub = build_epub(
        tmp_path / "missing-index.epub",
        EpubMetadata(title="Missing paragraph index"),
        [Chapter("Chapter 1", [
            Block("p", "First paragraph.", 1, -1, (0.1, 0.1, 0.9, 0.2)),
            Block("p", "Second paragraph.", 1, -1, (0.1, 0.3, 0.9, 0.4)),
        ])],
    )
    with zipfile.ZipFile(epub) as archive:
        chapter = archive.read("OEBPS/chapter_0001.xhtml")
        assert b'data-source-page=' not in chapter
        assert b'id="p-1--1"' not in chapter


def test_confident_cover_is_marked_in_opf_and_not_repeated_as_illustration(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    cover_path = _save_image(workspace, 1, 1, "red")
    epub = build_epub(
        tmp_path / "cover.epub",
        EpubMetadata(title="Cover metadata"),
        [Chapter("Chapter 1", [Block("p", "Book text.")])],
    )

    count = embed_pdf_images(
        epub,
        workspace,
        total_pages=1,
        page_classifications={
            1: {"type": "cover_candidate", "confidence": 0.78}
        },
    )

    assert count == 1
    with zipfile.ZipFile(epub) as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        cover_items = opf.findall(
            "opf:manifest/opf:item[@properties='cover-image']", NS
        )
        assert len(cover_items) == 1
        assert cover_items[0].get("href") == cover_path
        chapter = ET.fromstring(archive.read("OEBPS/chapter_0001.xhtml"))
        cover = chapter.find(".//x:figure[@class='pdf-cover']", NS)
        assert cover is not None
        assert cover.find("x:img", NS).get("src") == cover_path
        assert chapter.find(".//x:p", NS).text == "Book text."
        assert f"OEBPS/{cover_path}" in archive.namelist()


def test_advert_candidate_images_are_preserved_in_separate_appendix(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    advert_path = _save_image(workspace, 2, 1, "blue")
    epub = build_epub(
        tmp_path / "advert.epub",
        EpubMetadata(title="Advertisement handling"),
        [Chapter("Chapter 1", [
            Block("p", "Text before.", 2, 1, (0.1, 0.1, 0.9, 0.2)),
            Block("p", "Text after.", 2, 2, (0.1, 0.7, 0.9, 0.8)),
        ])],
    )

    assert embed_pdf_images(
        epub,
        workspace,
        total_pages=2,
        page_classifications={
            2: {"type": "advert_candidate", "confidence": 0.68}
        },
    ) == 1

    with zipfile.ZipFile(epub) as archive:
        chapter = archive.read("OEBPS/chapter_0001.xhtml")
        assert b"Promotional material" not in chapter
        assert b"Illustrations" not in chapter
        assert chapter.index(advert_path.encode("utf-8")) < chapter.index(b"Text before.")
        assert advert_path.encode("utf-8") in chapter
        assert f"OEBPS/{advert_path}" in archive.namelist()


def test_low_confidence_cover_candidate_is_not_promoted_or_removed(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    cover_path = _save_image(workspace, 1, 1, "green")
    epub = build_epub(
        tmp_path / "uncertain-cover.epub",
        EpubMetadata(title="Uncertain cover"),
        [Chapter("Chapter 1", [Block("p", "Book text.")])],
    )

    embed_pdf_images(
        epub,
        workspace,
        total_pages=1,
        page_classifications={
            1: {"type": "cover_candidate", "confidence": 0.60}
        },
    )

    with zipfile.ZipFile(epub) as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert not opf.findall(
            "opf:manifest/opf:item[@properties='cover-image']", NS
        )
        chapter = ET.fromstring(archive.read("OEBPS/chapter_0001.xhtml"))
        assert len(chapter.findall(".//x:figure", NS)) == 1
        assert f"OEBPS/{cover_path}" in archive.namelist()


def test_large_page_images_are_inserted_before_text_on_their_source_pages(tmp_path):
    workspace = JobWorkspace(tmp_path / "job")
    workspace.create()
    page_two_path = _save_image(workspace, 2, 1, "blue")
    page_three_path = _save_image(workspace, 3, 1, "green")
    epub = build_epub(
        tmp_path / "full-page-images.epub",
        EpubMetadata(title="Source page order"),
        [Chapter("Chapter 1", [
            Block("p", "Page two text.", 2, 1, (0.1, 0.1, 0.9, 0.2)),
            Block("p", "Page three text.", 3, 1, (0.1, 0.1, 0.9, 0.2)),
        ])],
    )

    assert embed_pdf_images(
        epub,
        workspace,
        total_pages=3,
        page_classifications={
            2: {"type": "advert_candidate", "confidence": 0.68},
            3: {"type": "advert_candidate", "confidence": 0.68},
        },
    ) == 2

    with zipfile.ZipFile(epub) as archive:
        chapter = archive.read("OEBPS/chapter_0001.xhtml")
        page_two_figure = chapter.index(page_two_path.encode("utf-8"))
        page_two_text = chapter.index(b"Page two text.")
        page_three_figure = chapter.index(page_three_path.encode("utf-8"))
        page_three_text = chapter.index(b"Page three text.")
        assert page_two_figure < page_two_text < page_three_figure < page_three_text
        assert b"Promotional material" not in chapter
        assert b"Illustrations" not in chapter
