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
    first_path = generate_job_epub(job, workspace, total_pages=1)
    original_mtime = first_path.stat().st_mtime_ns
    original_bytes = first_path.read_bytes()

    # Call again: should return same file without rewriting
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
