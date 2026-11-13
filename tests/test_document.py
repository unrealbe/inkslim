from conversion.document import (
    DOCUMENT_MODEL_VERSION,
    DocumentChapter,
    DocumentModel,
    DocumentParagraph,
    build_document_model,
)
from conversion.epub.builder import Block, Chapter, EpubMetadata


def _para(index, text, bbox=(0.1, 0.1, 0.9, 0.2)):
    return {
        "paragraph_index": index,
        "text": text,
        "bbox": list(bbox),
        "confidence": 95.0,
        "source": "native",
        "line_indices": [index],
    }


def test_build_document_model_with_explicit_chapters():
    cleaned_pages = {
        1: [
            _para(1, "CHAPTER 1"),
            _para(2, "This is the first sentence of chapter one."),
            _para(3, "SECTION 1.1"),
            _para(4, "More content under section."),
        ],
        2: [
            _para(1, "Continuing on page two."),
            _para(2, "CHAPTER 2: THE ADVENTURE"),
            _para(3, "This is chapter two content."),
        ],
    }
    structure_analysis = {
        "schema_version": 1,
        "total_pages": 2,
        "heading_count": 3,
        "chapter_count": 2,
        "chapters": [
            {
                "chapter_index": 1,
                "page_number": 1,
                "paragraph_index": 1,
                "title": "CHAPTER 1",
                "confidence": 0.98,
                "reason": "explicit_multilingual_chapter_label",
            },
            {
                "chapter_index": 2,
                "page_number": 2,
                "paragraph_index": 2,
                "title": "CHAPTER 2: THE ADVENTURE",
                "confidence": 0.98,
                "reason": "explicit_multilingual_chapter_label",
            },
        ],
    }
    page_annotations = {
        1: [
            {
                "paragraph_index": 1,
                "text": "CHAPTER 1",
                "kind": "chapter_heading",
                "chapter_start": True,
                "chapter_index": 1,
            },
            {
                "paragraph_index": 3,
                "text": "SECTION 1.1",
                "kind": "section_heading",
                "chapter_start": False,
            },
        ],
        2: [
            {
                "paragraph_index": 2,
                "text": "CHAPTER 2: THE ADVENTURE",
                "kind": "chapter_heading",
                "chapter_start": True,
                "chapter_index": 2,
            },
        ],
    }

    doc = build_document_model(
        cleaned_pages,
        structure_analysis,
        page_annotations,
        total_pages=2,
        title="My Book",
        language="en",
        author="Author Name",
    )

    assert isinstance(doc, DocumentModel)
    assert doc.metadata.title == "My Book"
    assert doc.metadata.language == "en"
    assert doc.metadata.author == "Author Name"
    assert len(doc.chapters) == 2

    # Chapter 1
    ch1 = doc.chapters[0]
    assert ch1.title == "CHAPTER 1"
    # Heading itself is not in paragraphs, only content (including text on page 2 before Chapter 2)
    assert len(ch1.paragraphs) == 4
    assert ch1.paragraphs[0].text == "This is the first sentence of chapter one."
    assert ch1.paragraphs[0].kind == "p"
    assert ch1.paragraphs[0].source_page == 1
    assert ch1.paragraphs[0].paragraph_index == 2
    assert ch1.paragraphs[0].bbox == (0.1, 0.1, 0.9, 0.2)
    assert ch1.paragraphs[1].text == "SECTION 1.1"
    assert ch1.paragraphs[1].kind == "h2"
    assert ch1.paragraphs[2].text == "More content under section."
    assert ch1.paragraphs[3].text == "Continuing on page two."
    assert ch1.paragraphs[3].kind == "p"

    # Chapter 2
    ch2 = doc.chapters[1]
    assert ch2.title == "CHAPTER 2: THE ADVENTURE"
    assert len(ch2.paragraphs) == 1
    assert ch2.paragraphs[0].text == "This is chapter two content."


def test_build_document_model_fallback_single_chapter():
    """Documents without chapter headings fall back to a single document-titled chapter."""
    cleaned_pages = {
        1: [
            _para(1, "Just a short essay."),
            _para(2, "Second paragraph of the essay."),
        ],
        2: [
            _para(1, "Conclusion on page two."),
        ],
    }
    structure_analysis = {
        "schema_version": 1,
        "total_pages": 2,
        "heading_count": 0,
        "chapter_count": 0,
        "chapters": [],
    }
    page_annotations = {1: [], 2: []}

    doc = build_document_model(
        cleaned_pages,
        structure_analysis,
        page_annotations,
        total_pages=2,
        title="Short Essay",
        language="nl",
    )

    assert len(doc.chapters) == 1
    assert doc.chapters[0].title == "Short Essay"
    assert len(doc.chapters[0].paragraphs) == 3
    assert doc.chapters[0].paragraphs[0].text == "Just a short essay."
    assert doc.chapters[0].paragraphs[2].text == "Conclusion on page two."


def test_build_document_model_promotes_display_headings():
    """When no explicit chapter labels exist, display headings are promoted to chapters."""
    cleaned_pages = {
        1: [
            _para(1, "PROLOGUE"),
            _para(2, "The world began in darkness."),
            _para(3, "PART ONE"),
            _para(4, "The sun rose above the mountains."),
        ],
    }
    structure_analysis = {
        "schema_version": 1,
        "total_pages": 1,
        "heading_count": 2,
        "chapter_count": 0,
        "chapters": [],
    }
    page_annotations = {
        1: [
            {
                "paragraph_index": 1,
                "text": "PROLOGUE",
                "kind": "section_heading",
                "chapter_start": False,
            },
            {
                "paragraph_index": 3,
                "text": "PART ONE",
                "kind": "display_heading",
                "chapter_start": False,
            },
        ],
    }

    doc = build_document_model(
        cleaned_pages,
        structure_analysis,
        page_annotations,
        total_pages=1,
        title="Epic Fantasy",
        language="en",
    )

    # Should be promoted into chapters
    assert len(doc.chapters) == 2
    assert doc.chapters[0].title == "PROLOGUE"
    assert doc.chapters[0].paragraphs[0].text == "The world began in darkness."
    assert doc.chapters[1].title == "PART ONE"
    assert doc.chapters[1].paragraphs[0].text == "The sun rose above the mountains."


def test_build_document_model_empty_document():
    doc = build_document_model(
        {},
        {"chapters": [], "heading_count": 0},
        {},
        total_pages=1,
        title="Empty Book",
    )
    assert len(doc.chapters) == 1
    assert doc.chapters[0].title == "Empty Book"
    assert len(doc.chapters[0].paragraphs) == 1


def test_to_epub_chapters():
    doc = DocumentModel(
        metadata=EpubMetadata(title="Test", language="en"),
        chapters=[
            DocumentChapter(
                title="Chapter 1",
                paragraphs=[
                    DocumentParagraph(text="Paragraph 1", kind="p"),
                    DocumentParagraph(text="Section 1", kind="h2"),
                ],
            )
        ],
    )
    epub_chapters = doc.to_epub_chapters()
    assert len(epub_chapters) == 1
    assert isinstance(epub_chapters[0], Chapter)
    assert epub_chapters[0].title == "Chapter 1"
    assert len(epub_chapters[0].blocks) == 2
    assert isinstance(epub_chapters[0].blocks[0], Block)
    assert epub_chapters[0].blocks[0].kind == "p"
    assert epub_chapters[0].blocks[0].text == "Paragraph 1"
    assert epub_chapters[0].blocks[1].kind == "h2"
    assert epub_chapters[0].blocks[1].text == "Section 1"


def test_document_model_dict_roundtrip():
    original = DocumentModel(
        metadata=EpubMetadata(
            title="Serialized Book",
            language="nl",
            author="Test Author",
            identifier="urn:uuid:12345",
            toc_title="Inhoud",
        ),
        chapters=[
            DocumentChapter(
                title="Hoofdstuk 1",
                paragraphs=[
                    DocumentParagraph(
                        text="Alinea 1", kind="p", source_page=3,
                        paragraph_index=7, bbox=(0.1, 0.2, 0.8, 0.3),
                    ),
                    DocumentParagraph(text="Kopje", kind="h2"),
                ],
            )
        ],
    )
    data = original.to_dict()
    assert data["schema_version"] == DOCUMENT_MODEL_VERSION
    assert data["metadata"]["title"] == "Serialized Book"
    assert data["metadata"]["toc_title"] == "Inhoud"

    restored = DocumentModel.from_dict(data)
    assert restored.metadata.title == original.metadata.title
    assert restored.metadata.language == original.metadata.language
    assert restored.metadata.author == original.metadata.author
    assert restored.metadata.identifier == original.metadata.identifier
    assert len(restored.chapters) == 1
    assert restored.chapters[0].title == "Hoofdstuk 1"
    assert len(restored.chapters[0].paragraphs) == 2
    assert restored.chapters[0].paragraphs[0].text == "Alinea 1"
    assert restored.chapters[0].paragraphs[0].kind == "p"
    assert restored.chapters[0].paragraphs[0].source_page == 3
    assert restored.chapters[0].paragraphs[0].paragraph_index == 7
    assert restored.chapters[0].paragraphs[0].bbox == (0.1, 0.2, 0.8, 0.3)
    assert restored.chapters[0].paragraphs[1].text == "Kopje"
    assert restored.chapters[0].paragraphs[1].kind == "h2"


def test_workspace_document_model_checkpoint(tmp_path):
    from conversion.storage import JobWorkspace

    workspace = JobWorkspace(tmp_path / "job-1")
    workspace.create()

    assert workspace.load_document_model() is None

    model = DocumentModel(
        metadata=EpubMetadata(title="Workspace Book", language="en"),
        chapters=[
            DocumentChapter(
                title="Ch 1",
                paragraphs=[DocumentParagraph(text="Hello world", kind="p")],
            )
        ],
    )
    saved_path = workspace.save_document_model(model)
    assert saved_path.is_file()

    loaded = workspace.load_document_model()
    assert loaded is not None
    assert loaded.metadata.title == "Workspace Book"
    assert len(loaded.chapters) == 1
    assert loaded.chapters[0].paragraphs[0].text == "Hello world"


def test_structure_recovery_after_partial_checkpoint_interruption(tmp_path):
    """Simulate worker interrupted after save_structure_analysis but before all page checkpoints."""
    from conversion.storage import JobWorkspace
    from conversion.structure import detect_document_structure

    workspace = JobWorkspace(tmp_path / "interrupted-job")
    workspace.create()

    p1 = [_para(1, "CHAPTER 1"), _para(2, "Content of chapter one.")]
    p2 = [_para(1, "CHAPTER 2"), _para(2, "Content of chapter two.")]
    workspace.save_page_cleaned_paragraphs(1, p1)
    workspace.save_page_cleaned_paragraphs(2, p2)

    structure, page_annotations = detect_document_structure({1: p1, 2: p2}, total_pages=2)
    workspace.save_structure_analysis(structure)
    workspace.save_page_structure(1, page_annotations[1])

    # Interruption: page 2 structure was NOT written
    assert workspace.load_page_structure(2) is None
    assert workspace.load_structure_analysis() is not None

    # Deterministic recovery logic
    total_pages = 2
    has_all_page_structures = all(
        workspace.load_page_structure(page_number) is not None
        for page_number in range(1, total_pages + 1)
    )
    assert not has_all_page_structures

    # Re-run detection deterministically from cleaned paragraphs
    cleaned_pages = {
        p: workspace.load_page_cleaned_paragraphs(p)
        for p in range(1, total_pages + 1)
    }
    recovered_structure, recovered_page_annotations = detect_document_structure(
        cleaned_pages, total_pages=total_pages
    )
    for p in range(1, total_pages + 1):
        if workspace.load_page_structure(p) is None:
            workspace.save_page_structure(p, recovered_page_annotations.get(p, []))

    # Assert page 2 is restored
    assert workspace.load_page_structure(2) is not None
    assert len(workspace.load_page_structure(2)) == 1
    assert workspace.load_page_structure(2)[0]["text"] == "CHAPTER 2"

    # Assert document model produces exact expected chapters
    doc = build_document_model(
        cleaned_pages,
        workspace.load_structure_analysis(),
        {p: workspace.load_page_structure(p) for p in range(1, total_pages + 1)},
        total_pages=2,
        title="Interrupted Book",
    )
    assert len(doc.chapters) == 2
    assert doc.chapters[0].title == "CHAPTER 1"
    assert doc.chapters[1].title == "CHAPTER 2"




def test_document_model_reads_legacy_schema_v1_without_source_geometry():
    legacy = {
        "schema_version": 1,
        "metadata": {"title": "Legacy", "language": "en"},
        "chapters": [{
            "title": "Chapter 1",
            "paragraphs": [{"text": "Legacy paragraph", "kind": "p"}],
        }],
    }
    restored = DocumentModel.from_dict(legacy)
    paragraph = restored.chapters[0].paragraphs[0]
    assert paragraph.text == "Legacy paragraph"
    assert paragraph.source_page is None
    assert paragraph.paragraph_index is None
    assert paragraph.bbox is None



def test_document_model_discards_malformed_source_bbox():
    data = {
        "schema_version": DOCUMENT_MODEL_VERSION,
        "metadata": {"title": "Malformed geometry", "language": "en"},
        "chapters": [{
            "title": "Chapter 1",
            "paragraphs": [{
                "text": "Text remains usable.",
                "kind": "p",
                "source_page": 1,
                "paragraph_index": 1,
                "bbox": [0.1, None, 0.9, 0.2],
            }],
        }],
    }
    restored = DocumentModel.from_dict(data)
    paragraph = restored.chapters[0].paragraphs[0]
    assert paragraph.text == "Text remains usable."
    assert paragraph.source_page == 1
    assert paragraph.paragraph_index == 1
    assert paragraph.bbox is None


def test_workspace_quarantines_document_model_with_unhashable_schema_version(tmp_path):
    import json

    from conversion.storage import JobWorkspace

    workspace = JobWorkspace(tmp_path / "malformed-schema")
    workspace.create()
    workspace.document_model_path.write_text(
        json.dumps({"schema_version": [], "chapters": []}),
        encoding="utf-8",
    )
    assert workspace.load_document_model() is None
    assert not workspace.document_model_path.exists()


def test_build_document_model_omits_only_explicitly_excluded_frontmatter_pages():
    cleaned_pages = {
        1: [_para(0, "Contents"), _para(1, "CHAPTER 1 ........ 5")],
        2: [_para(0, "CHAPTER 1"), _para(1, "The actual story begins here.")],
    }
    structure_analysis = {
        "schema_version": 2,
        "total_pages": 2,
        "heading_count": 1,
        "chapter_count": 1,
        "excluded_pages": [1],
        "chapters": [{
            "chapter_index": 1,
            "page_number": 2,
            "paragraph_index": 0,
            "title": "CHAPTER 1",
        }],
    }
    page_annotations = {
        1: [],
        2: [{
            "paragraph_index": 0,
            "text": "CHAPTER 1",
            "kind": "chapter_heading",
            "chapter_start": True,
        }],
    }

    doc = build_document_model(
        cleaned_pages, structure_analysis, page_annotations,
        total_pages=2, title="Example Book",
    )

    assert len(doc.chapters) == 1
    assert doc.chapters[0].title == "CHAPTER 1"
    assert [item.text for item in doc.chapters[0].paragraphs] == [
        "The actual story begins here."
    ]
