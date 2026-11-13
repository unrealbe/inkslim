import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from conversion.document import build_document_model
from conversion.epub.generator import generate_job_epub, is_valid_epub
from conversion.models import Job, JobStatus
from conversion.storage import JobWorkspace

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "x": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}


def _para(index, text, bbox=(0.1, 0.1, 0.9, 0.2)):
    return {
        "paragraph_index": index,
        "text": text,
        "bbox": list(bbox),
        "confidence": 95.0,
        "source": "native",
        "line_indices": [index],
    }


def _dummy_job(tmp_path, name="test-book.pdf", language="eng", job_id="11111111-1111-1111-1111-111111111111"):
    return Job(
        id=job_id,
        status=JobStatus.OCR,
        input_name=name,
        input_path=str(tmp_path / name),
        language=language,
    )



def _save_stitched_checkpoint(workspace, total_pages):
    pages = {
        page: workspace.load_page_cleaned_paragraphs(page) or []
        for page in range(1, total_pages + 1)
    }
    workspace.save_stitched_paragraphs(
        pages,
        {
            "schema_version": 1,
            "total_pages": total_pages,
            "stitched_count": 0,
            "stitched": [],
        },
    )

def test_generate_job_epub_with_multiple_chapters(tmp_path):
    workspace = JobWorkspace(tmp_path / "job-multi")
    workspace.create()

    p1 = [_para(1, "CHAPTER 1"), _para(2, "Eerste alinea van hoofdstuk 1.")]
    p2 = [_para(1, "CHAPTER 2: DE REIS"), _para(2, "Tweede hoofdstuk tekst.")]
    workspace.save_page_cleaned_paragraphs(1, p1)
    workspace.save_page_cleaned_paragraphs(2, p2)

    workspace.save_structure_analysis({
        "schema_version": 1,
        "total_pages": 2,
        "heading_count": 2,
        "chapter_count": 2,
        "chapters": [
            {"chapter_index": 1, "page_number": 1, "paragraph_index": 1, "title": "CHAPTER 1"},
            {"chapter_index": 2, "page_number": 2, "paragraph_index": 1, "title": "CHAPTER 2: DE REIS"},
        ],
    })
    workspace.save_page_structure(1, [{"paragraph_index": 1, "text": "CHAPTER 1", "kind": "chapter_heading", "chapter_start": True}])
    workspace.save_page_structure(2, [{"paragraph_index": 1, "text": "CHAPTER 2: DE REIS", "kind": "chapter_heading", "chapter_start": True}])

    job = _dummy_job(tmp_path, name="avontuur.pdf", language="nld")
    _save_stitched_checkpoint(workspace, 2)
    _save_stitched_checkpoint(workspace, 2)
    epub_path = generate_job_epub(job, workspace, total_pages=2)

    assert epub_path.is_file()
    assert is_valid_epub(epub_path)
    assert epub_path.name == "avontuur-11111111.epub"

    # Verify EPUB contents and XML well-formedness
    with zipfile.ZipFile(epub_path, "r") as archive:
        assert archive.infolist()[0].filename == "mimetype"
        assert archive.read("mimetype").strip() == b"application/epub+zip"

        # Check content.opf
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert opf.findtext("opf:metadata/dc:language", namespaces=NS) == "nl"
        assert opf.findtext("opf:metadata/dc:title", namespaces=NS) == "Avontuur"
        assert opf.findtext("opf:metadata/dc:identifier", namespaces=NS) == (
            "urn:uuid:11111111-1111-1111-1111-111111111111"
        )
        spine = [item.get("idref") for item in opf.findall("opf:spine/opf:itemref", NS)]
        assert len(spine) == 2

        # Check chapters
        ch1 = ET.fromstring(archive.read("OEBPS/chapter_0001.xhtml"))
        assert ch1.findtext(".//x:h1", namespaces=NS) == "CHAPTER 1"
        assert ch1.findtext(".//x:p", namespaces=NS) == "Eerste alinea van hoofdstuk 1."

        ch2 = ET.fromstring(archive.read("OEBPS/chapter_0002.xhtml"))
        assert ch2.findtext(".//x:h1", namespaces=NS) == "CHAPTER 2: DE REIS"
        assert ch2.findtext(".//x:p", namespaces=NS) == "Tweede hoofdstuk tekst."


def test_generate_job_epub_fallback_without_chapters(tmp_path):
    workspace = JobWorkspace(tmp_path / "job-fallback")
    workspace.create()

    p1 = [_para(1, "Gewone tekst zonder hoofdstukaanduiding.")]
    workspace.save_page_cleaned_paragraphs(1, p1)
    workspace.save_structure_analysis({
        "schema_version": 1,
        "total_pages": 1,
        "heading_count": 0,
        "chapter_count": 0,
        "chapters": [],
    })
    workspace.save_page_structure(1, [])

    job = _dummy_job(tmp_path, name="essay.pdf", language="eng")
    _save_stitched_checkpoint(workspace, 1)
    _save_stitched_checkpoint(workspace, 1)
    epub_path = generate_job_epub(job, workspace, total_pages=1)

    assert epub_path.is_file()
    assert is_valid_epub(epub_path)

    with zipfile.ZipFile(epub_path, "r") as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert opf.findtext("opf:metadata/dc:language", namespaces=NS) == "en"
        assert opf.findtext("opf:metadata/dc:title", namespaces=NS) == "Essay"

        ch1 = ET.fromstring(archive.read("OEBPS/chapter_0001.xhtml"))
        assert ch1.findtext(".//x:h1", namespaces=NS) == "Essay"
        assert ch1.findtext(".//x:p", namespaces=NS) == "Gewone tekst zonder hoofdstukaanduiding."


def test_generate_job_epub_unicode_and_special_characters(tmp_path):
    workspace = JobWorkspace(tmp_path / "job-unicode")
    workspace.create()

    text = "Marie & Jan spraken over café's, “geheimen” en de façade van het gebouw — prachtig!"
    p1 = [_para(1, text)]
    workspace.save_page_cleaned_paragraphs(1, p1)
    workspace.save_structure_analysis({
        "schema_version": 1,
        "total_pages": 1,
        "heading_count": 0,
        "chapter_count": 0,
        "chapters": [],
    })
    workspace.save_page_structure(1, [])

    job = _dummy_job(tmp_path, name="special-chars.pdf", language="eng")
    _save_stitched_checkpoint(workspace, 1)
    _save_stitched_checkpoint(workspace, 1)
    epub_path = generate_job_epub(job, workspace, total_pages=1)

    assert is_valid_epub(epub_path)
    with zipfile.ZipFile(epub_path, "r") as archive:
        ch = ET.fromstring(archive.read("OEBPS/chapter_0001.xhtml"))
        p = ch.findtext(".//x:p", namespaces=NS)
        assert "Marie & Jan" in p
        assert "café's" in p
        assert "façade" in p
        assert "— prachtig!" in p


def test_generate_job_epub_reuses_existing_valid_file(tmp_path):
    workspace = JobWorkspace(tmp_path / "job-reuse")
    workspace.create()

    p1 = [_para(1, "Eerste versie.")]
    workspace.save_page_cleaned_paragraphs(1, p1)
    workspace.save_structure_analysis({
        "schema_version": 1, "total_pages": 1, "heading_count": 0, "chapter_count": 0, "chapters": []
    })
    workspace.save_page_structure(1, [])

    job = _dummy_job(tmp_path, name="reuse.pdf")
    _save_stitched_checkpoint(workspace, 1)
    first_path = generate_job_epub(job, workspace, total_pages=1)
    original_mtime = first_path.stat().st_mtime_ns
    original_bytes = first_path.read_bytes()

    # Call again: should return same file without rewriting
    _save_stitched_checkpoint(workspace, 1)
    second_path = generate_job_epub(job, workspace, total_pages=1)
    assert second_path == first_path
    assert second_path.stat().st_mtime_ns == original_mtime
    assert second_path.read_bytes() == original_bytes


def test_is_valid_epub_rejects_corrupted_archives(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    assert not is_valid_epub(tmp_path / "nonexistent.epub")

    empty = tmp_path / "empty.epub"
    empty.write_bytes(b"")
    assert not is_valid_epub(empty)

    not_zip = tmp_path / "bad.epub"
    not_zip.write_bytes(b"This is just plain text, not a zip archive.")
    assert not is_valid_epub(not_zip)


def test_same_source_filename_gets_distinct_job_outputs_and_identifiers(tmp_path):
    job_ids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    outputs = []

    for index, job_id in enumerate(job_ids):
        workspace = JobWorkspace(tmp_path / f"job-{index}")
        workspace.create()
        workspace.save_page_cleaned_paragraphs(1, [_para(1, f"Versie {index}.")])
        workspace.save_structure_analysis({
            "schema_version": 1,
            "total_pages": 1,
            "heading_count": 0,
            "chapter_count": 0,
            "chapters": [],
        })
        workspace.save_page_structure(1, [])
        job = _dummy_job(tmp_path, name="same-book.pdf", job_id=job_id)
        _save_stitched_checkpoint(workspace, 1)
        output = generate_job_epub(job, workspace, total_pages=1)
        with zipfile.ZipFile(output) as archive:
            opf = ET.fromstring(archive.read("OEBPS/content.opf"))
            identifier = opf.findtext("opf:metadata/dc:identifier", namespaces=NS)
        outputs.append((output, identifier))

    assert outputs[0][0].name == "same-book-11111111.epub"
    assert outputs[1][0].name == "same-book-22222222.epub"
    assert outputs[0][0] != outputs[1][0]
    assert outputs[0][1] == f"urn:uuid:{job_ids[0]}"
    assert outputs[1][1] == f"urn:uuid:{job_ids[1]}"
    assert outputs[0][1] != outputs[1][1]



def test_epub_language_is_inferred_from_reconstructed_book_text(tmp_path):
    workspace = JobWorkspace(tmp_path / "job-language")
    workspace.create()
    english = (
        "The application processes every page of the book and reconstructs "
        "the original paragraphs in their natural reading order. "
    ) * 3
    for page_number in (1, 2):
        workspace.save_page_cleaned_paragraphs(page_number, [_para(1, english)])
        workspace.save_page_structure(page_number, [])
    workspace.save_structure_analysis({
        "schema_version": 1,
        "total_pages": 2,
        "heading_count": 0,
        "chapter_count": 0,
        "chapters": [],
    })

    job = _dummy_job(tmp_path, name="english-book.pdf", language="eng")
    _save_stitched_checkpoint(workspace, 2)
    _save_stitched_checkpoint(workspace, 2)
    epub_path = generate_job_epub(job, workspace, total_pages=2)

    with zipfile.ZipFile(epub_path, "r") as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert opf.findtext("opf:metadata/dc:language", namespaces=NS) == "en"

    diagnostics = workspace.load_document_language_analysis()
    assert diagnostics["language"] == "en"
    assert diagnostics["method"] == "weighted_reconstructed_page_language"
    assert diagnostics["supporting_pages"] == 2


def test_epub_metadata_uses_pdf_metadata_before_filename_fallback(tmp_path):
    import pikepdf

    workspace = JobWorkspace(tmp_path / "job-metadata")
    workspace.create()
    workspace.save_page_cleaned_paragraphs(1, [_para(1, "A paragraph of readable book text.")])
    workspace.save_structure_analysis({
        "schema_version": 1, "total_pages": 1, "heading_count": 0,
        "chapter_count": 0, "chapters": [],
    })
    workspace.save_page_structure(1, [])

    source = tmp_path / "The Long and Hungry Road - Filename Author.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.docinfo["/Title"] = "PDF Metadata Title"
        pdf.docinfo["/Author"] = "PDF Metadata Author"
        pdf.save(source)

    job = _dummy_job(tmp_path, name=source.name)
    _save_stitched_checkpoint(workspace, 1)
    _save_stitched_checkpoint(workspace, 1)
    epub_path = generate_job_epub(job, workspace, total_pages=1)

    with zipfile.ZipFile(epub_path) as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert opf.findtext("opf:metadata/dc:title", namespaces=NS) == "PDF Metadata Title"
        assert opf.findtext("opf:metadata/dc:creator", namespaces=NS) == "PDF Metadata Author"


def test_epub_metadata_falls_back_to_title_author_filename_pattern(tmp_path):
    import pikepdf

    workspace = JobWorkspace(tmp_path / "job-filename-metadata")
    workspace.create()
    workspace.save_page_cleaned_paragraphs(1, [_para(1, "A paragraph of readable book text.")])
    workspace.save_structure_analysis({
        "schema_version": 1, "total_pages": 1, "heading_count": 0,
        "chapter_count": 0, "chapters": [],
    })
    workspace.save_page_structure(1, [])

    source = tmp_path / "The Long and Hungry Road - Adrian Tchaikovsky.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.save(source)

    job = _dummy_job(tmp_path, name=source.name)
    _save_stitched_checkpoint(workspace, 1)
    _save_stitched_checkpoint(workspace, 1)
    epub_path = generate_job_epub(job, workspace, total_pages=1)

    with zipfile.ZipFile(epub_path) as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert opf.findtext("opf:metadata/dc:title", namespaces=NS) == "The Long and Hungry Road"
        assert opf.findtext("opf:metadata/dc:creator", namespaces=NS) == "Adrian Tchaikovsky"


def test_epub_metadata_sanitizes_pdf_title_and_author_before_fallback(tmp_path):
    import pikepdf

    workspace = JobWorkspace(tmp_path / "job-sanitized-metadata")
    workspace.create()
    workspace.save_page_cleaned_paragraphs(1, [_para(1, "A paragraph of readable book text.")])
    workspace.save_structure_analysis({
        "schema_version": 1, "total_pages": 1, "heading_count": 0,
        "chapter_count": 0, "chapters": [],
    })
    workspace.save_page_structure(1, [])

    source = tmp_path / "Fallback Title - Fallback Author.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.docinfo["/Title"] = "\x01"
        pdf.docinfo["/Author"] = "\x0b"
        pdf.save(source)

    job = _dummy_job(tmp_path, name=source.name)
    _save_stitched_checkpoint(workspace, 1)
    _save_stitched_checkpoint(workspace, 1)
    epub_path = generate_job_epub(job, workspace, total_pages=1)

    with zipfile.ZipFile(epub_path) as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert opf.findtext("opf:metadata/dc:title", namespaces=NS) == "Fallback Title"
        assert opf.findtext("opf:metadata/dc:creator", namespaces=NS) == "Fallback Author"


def test_cached_epub_is_rebuilt_when_pdf_metadata_changes(tmp_path):
    import pikepdf

    workspace = JobWorkspace(tmp_path / "job-metadata-cache")
    workspace.create()
    workspace.save_page_cleaned_paragraphs(1, [_para(1, "A paragraph of readable book text.")])
    workspace.save_structure_analysis({
        "schema_version": 1, "total_pages": 1, "heading_count": 0,
        "chapter_count": 0, "chapters": [],
    })
    workspace.save_page_structure(1, [])

    source = tmp_path / "Cached Book - Cached Author.pdf"
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        pdf.docinfo["/Title"] = "First PDF Title"
        pdf.docinfo["/Author"] = "First PDF Author"
        pdf.save(source)

    job = _dummy_job(tmp_path, name=source.name)
    _save_stitched_checkpoint(workspace, 1)
    first_epub = generate_job_epub(job, workspace, total_pages=1)
    with zipfile.ZipFile(first_epub) as archive:
        first_opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert first_opf.findtext("opf:metadata/dc:title", namespaces=NS) == "First PDF Title"

    updated_source = tmp_path / "updated-source.pdf"
    with pikepdf.open(source) as pdf:
        pdf.docinfo["/Title"] = "Updated PDF Title"
        pdf.docinfo["/Author"] = "Updated PDF Author"
        pdf.save(updated_source)
    updated_source.replace(source)

    second_epub = generate_job_epub(job, workspace, total_pages=1)
    assert second_epub == first_epub
    with zipfile.ZipFile(second_epub) as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        assert opf.findtext("opf:metadata/dc:title", namespaces=NS) == "Updated PDF Title"
        assert opf.findtext("opf:metadata/dc:creator", namespaces=NS) == "Updated PDF Author"



def test_cached_epub_is_rebuilt_when_paragraph_layout_is_outdated(tmp_path):
    workspace = JobWorkspace(tmp_path / "job-old-layout")
    workspace.create()
    workspace.save_page_cleaned_paragraphs(1, [_para(1, "A readable paragraph.")])
    workspace.save_structure_analysis({
        "schema_version": 1, "total_pages": 1, "heading_count": 0,
        "chapter_count": 0, "chapters": [],
    })
    workspace.save_page_structure(1, [])
    job = _dummy_job(tmp_path, name="old-layout.pdf")
    _save_stitched_checkpoint(workspace, 1)
    _save_stitched_checkpoint(workspace, 1)
    epub_path = generate_job_epub(job, workspace, total_pages=1)

    # Simulate an EPUB produced by the previous stylesheet version.
    with zipfile.ZipFile(epub_path, "r") as source:
        entries = {
            name: source.read(name)
            for name in source.namelist()
            if name != "OEBPS/styles.css"
        }
    entries["OEBPS/styles.css"] = (
        b"body { line-height: 1.4; }\\n"
        b"p { margin: 0; text-indent: 1.2em; }\\n"
    )
    outdated = epub_path.with_suffix(".outdated")
    with zipfile.ZipFile(outdated, "w") as target:
        for name, content in entries.items():
            target.writestr(name, content)
    outdated.replace(epub_path)

    _save_stitched_checkpoint(workspace, 1)
    rebuilt = generate_job_epub(job, workspace, total_pages=1)

    with zipfile.ZipFile(rebuilt) as archive:
        stylesheet = archive.read("OEBPS/styles.css").decode("utf-8")
    assert "Inkslim EPUB layout version: 4" in stylesheet
    assert "p { margin: 0 0 0.8em; text-indent: 0; }" in stylesheet



def test_cover_candidate_ocr_text_is_omitted_but_other_pages_remain():
    from conversion.epub.generator import _exclude_cover_ocr_text

    cleaned = {
        1: [_para(1, "4 x Pa > a x oe I z m")],
        2: [_para(1, "Multiple formats available")],
        3: [_para(1, "The actual story begins here.")],
    }
    classifications = {
        1: {
            "type": "cover_candidate",
            "confidence": 0.91,
            "signals": {
                "text_source": "tesseract",
                "text_word_count": 11,
                "largest_image_coverage": 1.0,
            },
        }
    }

    result, omitted = _exclude_cover_ocr_text(cleaned, classifications)

    assert omitted == [1]
    assert 1 not in result
    assert result[2] == cleaned[2]
    assert result[3] == cleaned[3]


def test_cover_candidate_filter_does_not_omit_native_or_text_rich_pages():
    from conversion.epub.generator import _exclude_cover_ocr_text

    cleaned = {1: [_para(1, "A meaningful title and author.")]}
    native_cover = {
        1: {
            "type": "cover_candidate",
            "signals": {
                "text_source": "native",
                "text_word_count": 5,
                "largest_image_coverage": 0.9,
            },
        }
    }
    text_rich_cover = {
        1: {
            "type": "cover_candidate",
            "signals": {
                "text_source": "tesseract",
                "text_word_count": 25,
                "largest_image_coverage": 0.9,
            },
        }
    }

    assert _exclude_cover_ocr_text(cleaned, native_cover) == (cleaned, [])
    assert _exclude_cover_ocr_text(cleaned, text_rich_cover) == (cleaned, [])


def test_cover_candidate_filter_requires_confident_image_extraction_and_multiple_pages():
    from conversion.epub.generator import _exclude_cover_ocr_text

    cleaned = {
        1: [_para(1, "4 x Pa > a x oe I z m")],
        2: [_para(1, "The actual story begins here.")],
    }
    low_confidence = {
        1: {
            "type": "cover_candidate",
            "confidence": 0.74,
            "signals": {
                "text_source": "tesseract",
                "text_word_count": 11,
                "largest_image_coverage": 1.0,
            },
        }
    }
    confident = {
        1: {
            "type": "cover_candidate",
            "confidence": 0.75,
            "signals": {
                "text_source": "tesseract",
                "text_word_count": 11,
                "largest_image_coverage": 1.0,
            },
        }
    }

    # If image extraction is incomplete, retain OCR text so it can anchor
    # the image-placement fallback instead of moving the cover to an appendix.
    assert _exclude_cover_ocr_text(cleaned, low_confidence) == (cleaned, [])
    assert _exclude_cover_ocr_text(cleaned, confident) == (
        {2: cleaned[2]},
        [1],
    )

    # A one-page cover must not become an empty document after filtering.
    one_page = {1: cleaned[1]}
    assert _exclude_cover_ocr_text(one_page, confident) == (one_page, [])



def test_cross_page_stitching_and_contents_exclusion_survive_epub_generation(tmp_path):
    """Exercise page stitching, structure detection and EPUB output as one pipeline."""
    from conversion.stitching import stitch_page_paragraphs
    from conversion.structure import detect_document_structure

    workspace = JobWorkspace(tmp_path / "job-cross-page-regression")
    workspace.create()

    contents = [
        _para(0, "Contents"),
        _para(1, "4 x Pa > a x oe I z m ........ 4"),
        _para(2, "CHAPTER 1 The Beginning ........ 5"),
        _para(3, "CHAPTER 2 The Journey ........ 12"),
    ]
    page_two = [
        _para(0, "CHAPTER 1", (0.1, 0.08, 0.9, 0.14)),
        _para(
            1,
            "The fleet’s instinctive response to these delicate tremors",
            (0.1, 0.72, 0.9, 0.94),
        ),
    ]
    page_three = [
        _para(
            0,
            "is to reach out and pull the ship into alignment.",
            (0.1, 0.04, 0.9, 0.22),
        ),
        _para(1, "A new paragraph starts here.", (0.1, 0.3, 0.9, 0.42)),
    ]
    cleaned_pages = {1: contents, 2: page_two, 3: page_three}
    for page_number, paragraphs in cleaned_pages.items():
        workspace.save_page_cleaned_paragraphs(page_number, paragraphs)

    page_context = {
        page_number: {"decision": "use_native", "image_count": 0}
        for page_number in range(1, 4)
    }
    stitched_pages, stitching_analysis = stitch_page_paragraphs(
        cleaned_pages, page_context, total_pages=3
    )
    assert stitching_analysis["stitched_count"] == 1
    assert stitched_pages[2][-1]["text"] == (
        "The fleet’s instinctive response to these delicate tremors "
        "is to reach out and pull the ship into alignment."
    )
    assert stitched_pages[3] == page_three[1:]

    structure, annotations = detect_document_structure(
        stitched_pages, total_pages=3
    )
    assert structure["excluded_pages"] == [1]
    assert structure["chapter_count"] == 1
    assert structure["chapters"][0]["title"] == "CHAPTER 1"
    assert annotations[1] == []

    workspace.save_stitched_paragraphs(stitched_pages, stitching_analysis)
    workspace.save_structure_analysis(structure)
    for page_number in range(1, 4):
        workspace.save_page_structure(page_number, annotations[page_number])

    job = _dummy_job(tmp_path, name="cross-page-regression.pdf", language="eng")
    epub_path = generate_job_epub(job, workspace, total_pages=3)

    assert is_valid_epub(epub_path)
    with zipfile.ZipFile(epub_path, "r") as archive:
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        chapter_files = [
            item.get("href")
            for item in opf.findall("opf:manifest/opf:item", NS)
            if item.get("media-type") == "application/xhtml+xml"
            and item.get("href", "").startswith("chapter_")
        ]
        assert chapter_files == ["chapter_0001.xhtml"]

        nav = archive.read("OEBPS/nav.xhtml").decode("utf-8")
        chapter = ET.fromstring(archive.read("OEBPS/chapter_0001.xhtml"))
        paragraphs = chapter.findall(".//x:p", NS)
        paragraph_texts = ["".join(node.itertext()) for node in paragraphs]

    assert "4 x Pa > a x oe I z m" not in nav
    assert "CHAPTER 2 The Journey" not in nav
    assert paragraph_texts == [
        "The fleet’s instinctive response to these delicate tremors "
        "is to reach out and pull the ship into alignment.",
        "A new paragraph starts here.",
    ]
