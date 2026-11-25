"""Tests for Gemini AI structure refinement and chapter classification."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from conversion.gemini import (
    DEFAULT_GEMINI_MODEL,
    GeminiApiError,
    apply_gemini_structure_refinement,
    build_structure_refinement_prompt,
    call_gemini_generate_content,
    extract_gemini_json_payload,
    extract_structure_candidates,
    query_gemini_structure,
)
from conversion.pdf.models import PageAnalysis, PageDecision
from conversion.storage import JobWorkspace


def test_extract_gemini_json_payload_valid():
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": json.dumps({"chapters": [{"page_number": 1, "paragraph_index": 0, "title": "Ch 1"}]})}
                    ]
                }
            }
        ]
    }
    data = extract_gemini_json_payload(response)
    assert data["chapters"][0]["title"] == "Ch 1"


def test_extract_gemini_json_payload_markdown_code_block():
    text = "```json\n{\"book_title\": \"Test Book\", \"chapters\": []}\n```"
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}]
                }
            }
        ]
    }
    data = extract_gemini_json_payload(response)
    assert data["book_title"] == "Test Book"
    assert data["chapters"] == []


def test_extract_gemini_json_payload_errors():
    with pytest.raises(GeminiApiError, match="no candidate"):
        extract_gemini_json_payload({})

    with pytest.raises(GeminiApiError, match="no content"):
        extract_gemini_json_payload({"candidates": [{}]})

    with pytest.raises(GeminiApiError, match="no parts"):
        extract_gemini_json_payload({"candidates": [{"content": {}}]})

    with pytest.raises(GeminiApiError, match="not valid JSON"):
        extract_gemini_json_payload({"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]})

    with pytest.raises(GeminiApiError, match="must be a JSON object"):
        extract_gemini_json_payload({"candidates": [{"content": {"parts": [{"text": "[1, 2]"}]}}]})


def test_call_gemini_generate_content_success():
    fake_body = json.dumps({
        "candidates": [{"content": {"parts": [{"text": "{\"confidence\": 0.9}"}]}}]
    }).encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = fake_body
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = call_gemini_generate_content("hello", api_key="secret-key")
        assert "candidates" in result
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "key=secret-key" in req.full_url
        assert req.get_method() == "POST"
        assert req.headers["Content-type"] == "application/json"


def test_call_gemini_generate_content_http_error():
    fp = io.BytesIO(b'{"error": {"message": "Invalid API key"}}')
    http_err = urllib.error.HTTPError(
        url="https://example.com", code=403, msg="Forbidden", hdrs={}, fp=fp
    )

    with patch("urllib.request.urlopen", side_effect=http_err):
        with pytest.raises(GeminiApiError, match="Gemini HTTP 403 error"):
            call_gemini_generate_content("test", api_key="bad-key")


def test_call_gemini_generate_content_network_error():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("Request timed out")):
        with pytest.raises(GeminiApiError, match="timed out"):
            call_gemini_generate_content("test", api_key="key")


def test_call_gemini_generate_content_validates_inputs():
    with pytest.raises(ValueError, match="api_key cannot be empty"):
        call_gemini_generate_content("test", api_key="")
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        call_gemini_generate_content("test", api_key="key", timeout_seconds=0)


def test_extract_structure_candidates():
    stitched_pages = {
        1: [
            {"paragraph_index": 0, "text": "Contents", "bbox": [0.1, 0.1, 0.9, 0.15]},
            {"paragraph_index": 1, "text": "Chapter 1 ... 5", "bbox": [0.1, 0.2, 0.9, 0.25]},
        ],
        2: [
            {"paragraph_index": 0, "text": "Chapter 1: The Gathering", "bbox": [0.1, 0.1, 0.9, 0.15]},
            {"paragraph_index": 1, "text": "The wind was rising across the moor.", "bbox": [0.1, 0.2, 0.9, 0.3]},
            {"paragraph_index": 2, "text": "Short note", "bbox": [0.1, 0.35, 0.5, 0.4]},
        ],
    }
    structure_analysis = {
        "excluded_pages": [1],
        "chapters": [{"page_number": 2, "paragraph_index": 0, "title": "Chapter 1: The Gathering"}],
    }
    page_annotations = {
        1: [],
        2: [
            {
                "paragraph_index": 0,
                "text": "Chapter 1: The Gathering",
                "kind": "chapter_heading",
                "chapter_start": True,
            }
        ],
    }

    candidates = extract_structure_candidates(
        stitched_pages, structure_analysis, page_annotations, total_pages=2
    )

    # Page 1 is excluded, so only page 2 candidates are present
    assert all(c["page_number"] == 2 for c in candidates)
    # The existing chapter start is retained with role="chapter_start"
    p0 = next(c for c in candidates if c["paragraph_index"] == 0)
    assert p0["role"] == "chapter_start"
    assert "The Gathering" in p0["text"]


def test_build_structure_refinement_prompt():
    candidates = [
        {"page_number": 2, "paragraph_index": 0, "text": "Chapter 1", "role": "chapter_start"},
        {"page_number": 10, "paragraph_index": 0, "text": "Chapter 2", "role": "heading"},
    ]
    prompt = build_structure_refinement_prompt(
        candidates, total_pages=20, existing_chapters=[{"page_number": 2, "paragraph_index": 0, "title": "Chapter 1"}], title="My Book"
    )
    assert "My Book" in prompt
    assert "Total Pages: 20" in prompt
    assert "Page 2, p_idx 0 [chapter_start]: Chapter 1" in prompt


def test_apply_gemini_structure_refinement():
    stitched_pages = {
        1: [
            {"paragraph_index": 0, "text": "PROLOGUE", "bbox": [0.1, 0.1, 0.9, 0.15]},
            {"paragraph_index": 1, "text": "Old tales told in shadows.", "bbox": [0.1, 0.2, 0.9, 0.4]},
        ],
        2: [
            {"paragraph_index": 0, "text": "1. THE BEGINNING", "bbox": [0.1, 0.1, 0.9, 0.15]},
            {"paragraph_index": 1, "text": "It started in the spring.", "bbox": [0.1, 0.2, 0.9, 0.4]},
        ],
        3: [
            {"paragraph_index": 0, "text": "FALSE ALARM NOISE", "bbox": [0.1, 0.1, 0.9, 0.15]},
            {"paragraph_index": 1, "text": "More story text.", "bbox": [0.1, 0.2, 0.9, 0.4]},
        ],
    }
    # Initial rule-based structure had a false positive on page 3
    structure_analysis = {
        "schema_version": 2,
        "total_pages": 3,
        "page_roles": {},
        "excluded_pages": [],
        "exclusion_reasons": {},
        "heading_count": 2,
        "chapter_count": 2,
        "chapters": [
            {"chapter_index": 1, "page_number": 2, "paragraph_index": 0, "title": "1. THE BEGINNING", "confidence": 0.88, "reason": "numbered"},
            {"chapter_index": 2, "page_number": 3, "paragraph_index": 0, "title": "FALSE ALARM NOISE", "confidence": 0.78, "reason": "caps"},
        ],
    }
    page_annotations = {
        1: [{"paragraph_index": 0, "text": "PROLOGUE", "kind": "section_heading", "chapter_start": False}],
        2: [{"paragraph_index": 0, "text": "1. THE BEGINNING", "kind": "chapter_heading", "chapter_start": True, "chapter_index": 1}],
        3: [{"paragraph_index": 0, "text": "FALSE ALARM NOISE", "kind": "chapter_heading", "chapter_start": True, "chapter_index": 2}],
    }

    gemini_result = {
        "book_title": "The Chronicles",
        "book_author": "Jane Doe",
        "chapters": [
            {"page_number": 1, "paragraph_index": 0, "title": "Prologue", "reason": "identifies prologue as starting section"},
            {"page_number": 2, "paragraph_index": 0, "title": "Chapter 1: The Beginning", "reason": "numbered chapter start"},
        ],
        "confidence": 0.96,
        "reasoning": "Detected prologue and chapter 1, discarded noise on page 3",
    }

    updated_structure, updated_annotations = apply_gemini_structure_refinement(
        stitched_pages, structure_analysis, page_annotations, gemini_result, total_pages=3
    )

    assert updated_structure["ai_refined"] is True
    assert updated_structure["chapter_count"] == 2
    assert updated_structure["suggested_title"] == "The Chronicles"
    assert updated_structure["suggested_author"] == "Jane Doe"
    assert [ch["title"] for ch in updated_structure["chapters"]] == [
        "Prologue", "Chapter 1: The Beginning"
    ]

    # Page 1 now has chapter_start=True
    assert updated_annotations[1][0]["chapter_start"] is True
    assert updated_annotations[1][0]["chapter_index"] == 1

    # Page 2 has chapter_start=True
    assert updated_annotations[2][0]["chapter_start"] is True
    assert updated_annotations[2][0]["chapter_index"] == 2

    # Page 3 false alarm is demoted to section_heading with chapter_start=False
    assert updated_annotations[3][0]["chapter_start"] is False
    assert updated_annotations[3][0]["kind"] == "section_heading"


def test_apply_gemini_structure_refinement_rejects_invalid_locations():
    stitched_pages = {
        1: [{"paragraph_index": 0, "text": "Introduction"}],
    }
    structure_analysis = {
        "chapters": [{"chapter_index": 1, "page_number": 1, "paragraph_index": 0, "title": "Intro"}],
        "excluded_pages": [],
    }
    page_annotations = {
        1: [{"paragraph_index": 0, "text": "Introduction", "kind": "chapter_heading", "chapter_start": True}],
    }

    # Gemini proposed out-of-range page numbers and invalid indices
    gemini_result = {
        "chapters": [
            {"page_number": 99, "paragraph_index": 0, "title": "Ghost Chapter"},
            {"page_number": 1, "paragraph_index": 42, "title": "Out of Bounds"},
            {"page_number": 1, "paragraph_index": 0, "title": ""},  # empty title
        ],
    }

    updated_structure, updated_annotations = apply_gemini_structure_refinement(
        stitched_pages, structure_analysis, page_annotations, gemini_result, total_pages=1
    )

    # 0 valid chapters -> retains existing structure
    assert updated_structure["ai_refined"] is False
    assert updated_structure["ai_skipped_reason"] == "no_valid_chapters_returned"
    assert updated_structure["chapters"][0]["title"] == "Intro"


def test_workspace_ai_structure_checkpoint(tmp_path):
    workspace = JobWorkspace(tmp_path / "job-1")
    workspace.create()

    assert workspace.load_ai_structure_analysis() is None

    sample_analysis = {
        "book_title": "Test Book",
        "chapters": [{"page_number": 1, "paragraph_index": 0, "title": "Ch 1"}],
    }
    saved_path = workspace.save_ai_structure_analysis(sample_analysis)
    assert saved_path.exists()

    loaded = workspace.load_ai_structure_analysis()
    assert loaded == sample_analysis


def test_query_gemini_structure_integration():
    stitched_pages = {
        1: [{"paragraph_index": 0, "text": "Chapter 1: Begin", "bbox": [0.1, 0.1, 0.9, 0.15]}],
    }
    structure_analysis = {
        "chapters": [{"page_number": 1, "paragraph_index": 0, "title": "Chapter 1: Begin"}],
        "excluded_pages": [],
    }
    page_annotations = {
        1: [{"paragraph_index": 0, "text": "Chapter 1: Begin", "kind": "chapter_heading", "chapter_start": True}],
    }

    fake_response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps({
                                "book_title": "Verified Book",
                                "chapters": [
                                    {"page_number": 1, "paragraph_index": 0, "title": "Chapter 1: The Beginning"}
                                ],
                                "confidence": 0.98,
                            })
                        }
                    ]
                }
            }
        ]
    }

    with patch("conversion.gemini.call_gemini_generate_content", return_value=fake_response):
        result = query_gemini_structure(
            stitched_pages,
            structure_analysis,
            page_annotations,
            total_pages=1,
            api_key="mock-api-key",
            model="gemini-2.5-flash",
        )
        assert result["book_title"] == "Verified Book"
        assert result["chapters"][0]["title"] == "Chapter 1: The Beginning"


def _make_page_analysis(page_number: int, decision: PageDecision) -> PageAnalysis:
    return PageAnalysis(
        page_number=page_number,
        width=612,
        height=792,
        character_count=100,
        word_count=20,
        image_count=0,
        has_native_text=True,
        native_text_quality=0.95,
        decision=decision,
    )


def test_worker_gemini_structure_pipeline_and_caching(tmp_path):
    from conversion.models import Job, JobStatus, PageMetrics, PageResult, QualityStatus, TextBlock, TextSource
    from conversion.pdf.models import PageAnalysis, PageDecision
    from conversion.pdf.ocr_processor import PdfOcrJobProcessor
    from conversion.storage import JobStore

    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    dummy_input = tmp_path / "test.pdf"
    dummy_input.write_bytes(b"%PDF-1.4\n%dummy\n")
    job = store.create_job(dummy_input, "test.pdf")

    workspace = store.workspace(job.id)
    workspace.create()

    # Pre-populate checkpoints for a 2-page document
    for page in (1, 2):
        workspace.save_page_analysis(
            _make_page_analysis(page, PageDecision.USE_NATIVE)
        )
        workspace.save_page_result(
            PageResult(
                page_number=page,
                width=612,
                height=792,
                blocks=[
                    TextBlock(
                        text=f"Chapter {page}" if page == 1 else "Story continues",
                        bbox=(0.1, 0.1, 0.9, 0.2),
                        block_type="text",
                        confidence=95.0,
                        source=TextSource.NATIVE,
                        line_number=1,
                        paragraph_number=1,
                    )
                ],
                source=TextSource.NATIVE,
                quality=QualityStatus.HIGH,
            )
        )
        workspace.save_page_paragraphs(
            page,
            [
                {
                    "paragraph_index": 0,
                    "text": f"Chapter {page}: Title" if page == 1 else "Story continues on page 2",
                    "bbox": [0.1, 0.1, 0.9, 0.2],
                    "confidence": 95.0,
                    "source": "native",
                }
            ],
        )
        workspace.save_page_dehyphenated(
            page,
            [
                {
                    "paragraph_index": 0,
                    "text": f"Chapter {page}: Title" if page == 1 else "Story continues on page 2",
                    "bbox": [0.1, 0.1, 0.9, 0.2],
                    "confidence": 95.0,
                    "source": "native",
                }
            ],
            removed_soft_hyphens=0,
        )
        workspace.save_page_cleaned_paragraphs(
            page,
            [
                {
                    "paragraph_index": 0,
                    "text": f"Chapter {page}: Title" if page == 1 else "Story continues on page 2",
                    "bbox": [0.1, 0.1, 0.9, 0.2],
                    "confidence": 95.0,
                    "source": "native",
                }
            ],
        )

    workspace.save_marginalia_analysis(
        {"schema_version": 2, "total_pages": 2, "items": [], "removed_paragraphs": 0, "pages_with_removals": [], "removed": []}
    )
    workspace.save_stitched_paragraphs(
        {
            1: [{"paragraph_index": 0, "text": "Chapter 1: Title", "bbox": [0.1, 0.1, 0.9, 0.2]}],
            2: [{"paragraph_index": 0, "text": "Story continues on page 2", "bbox": [0.1, 0.1, 0.9, 0.2]}],
        },
        {"schema_version": 1, "total_pages": 2, "stitched_cross_page_paragraphs": 0, "stitched_transitions": []},
    )

    mock_doc = MagicMock()
    mock_doc.__enter__.return_value = mock_doc
    mock_doc.__len__.return_value = 2

    gemini_data = {
        "book_title": "AI Book Title",
        "chapters": [
            {"page_number": 1, "paragraph_index": 0, "title": "Chapter 1: Title"},
            {"page_number": 2, "paragraph_index": 0, "title": "Chapter 2: Continuation"},
        ],
        "confidence": 0.95,
    }

    processor = PdfOcrJobProcessor(
        gemini_api_key="secret-api-key",
        gemini_model="gemini-2.5-flash",
    )

    with patch("conversion.pdf.ocr_processor.open_pdf_document", return_value=mock_doc), \
         patch("conversion.gemini.query_gemini_structure", return_value=gemini_data) as mock_query, \
         patch("conversion.pdf.ocr_processor.generate_job_epub", return_value=tmp_path / "out.epub"), \
         patch("conversion.pdf.ocr_processor.build_quality_report", return_value=tmp_path / "report.json"):

        result = processor(job, workspace, store)
        assert result.next_status == JobStatus.COMPLETED
        mock_query.assert_called_once()

        # Verify AI structure checkpoint was written
        ai_ckpt = workspace.load_ai_structure_analysis()
        assert ai_ckpt == gemini_data

        # Verify structure analysis checkpoint has ai_refined = True
        struct = workspace.load_structure_analysis()
        assert struct["ai_refined"] is True
        assert struct["chapter_count"] == 2

    # Second run: test that cached AI checkpoint is used without re-querying Gemini
    # Simulate a structure rebuild by deleting page 2 structure
    workspace.page_structure_path(2).unlink()
    assert workspace.load_page_structure(2) is None

    with patch("conversion.pdf.ocr_processor.open_pdf_document", return_value=mock_doc), \
         patch("conversion.gemini.query_gemini_structure") as mock_query_again, \
         patch("conversion.pdf.ocr_processor.generate_job_epub", return_value=tmp_path / "out.epub"), \
         patch("conversion.pdf.ocr_processor.build_quality_report", return_value=tmp_path / "report.json"):

        result2 = processor(job, workspace, store)
        assert result2.next_status == JobStatus.COMPLETED
        mock_query_again.assert_not_called()

        # Restored structure analysis still reflects AI refinement
        struct2 = workspace.load_structure_analysis()
        assert struct2["ai_refined"] is True
        assert struct2["chapter_count"] == 2
        p2_annot = workspace.load_page_structure(2)
        assert p2_annot[0]["chapter_start"] is True


@pytest.mark.parametrize(
    "gemini_response",
    [
        GeminiApiError("429 Too Many Requests"),
        {
            "chapters": [
                {"page_number": 1, "paragraph_index": 0, "title": "AI chapter"}
            ],
            "confidence": "not-a-number",
        },
    ],
    ids=["api-error", "invalid-confidence"],
)
def test_worker_gemini_structure_fallback_on_unusable_response(tmp_path, gemini_response):
    from conversion.models import Job, JobStatus, PageMetrics, PageResult, QualityStatus, TextBlock, TextSource
    from conversion.pdf.models import PageAnalysis, PageDecision
    from conversion.pdf.ocr_processor import PdfOcrJobProcessor
    from conversion.storage import JobStore

    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    dummy_input = tmp_path / "test.pdf"
    dummy_input.write_bytes(b"%PDF-1.4\n%dummy\n")
    job = store.create_job(dummy_input, "test.pdf")

    workspace = store.workspace(job.id)
    workspace.create()

    for page in (1,):
        workspace.save_page_analysis(
            _make_page_analysis(page, PageDecision.USE_NATIVE)
        )
        workspace.save_page_result(
            PageResult(
                page_number=page,
                width=612,
                height=792,
                blocks=[
                    TextBlock(
                        text="Chapter 1",
                        bbox=(0.1, 0.1, 0.9, 0.2),
                        block_type="text",
                        confidence=95.0,
                        source=TextSource.NATIVE,
                        line_number=1,
                        paragraph_number=1,
                    )
                ],
                source=TextSource.NATIVE,
                quality=QualityStatus.HIGH,
            )
        )
        workspace.save_page_paragraphs(
            page,
            [
                {
                    "paragraph_index": 0,
                    "text": "Chapter 1: Native Chapter",
                    "bbox": [0.1, 0.1, 0.9, 0.2],
                    "confidence": 95.0,
                    "source": "native",
                }
            ],
        )
        workspace.save_page_dehyphenated(
            page,
            [
                {
                    "paragraph_index": 0,
                    "text": "Chapter 1: Native Chapter",
                    "bbox": [0.1, 0.1, 0.9, 0.2],
                    "confidence": 95.0,
                    "source": "native",
                }
            ],
            removed_soft_hyphens=0,
        )
        workspace.save_page_cleaned_paragraphs(
            page,
            [
                {
                    "paragraph_index": 0,
                    "text": "Chapter 1: Native Chapter",
                    "bbox": [0.1, 0.1, 0.9, 0.2],
                    "confidence": 95.0,
                    "source": "native",
                }
            ],
        )

    workspace.save_marginalia_analysis(
        {"schema_version": 2, "total_pages": 1, "items": [], "removed_paragraphs": 0, "pages_with_removals": [], "removed": []}
    )
    workspace.save_stitched_paragraphs(
        {1: [{"paragraph_index": 0, "text": "Chapter 1: Native Chapter", "bbox": [0.1, 0.1, 0.9, 0.2]}]},
        {"schema_version": 1, "total_pages": 1, "stitched_cross_page_paragraphs": 0, "stitched_transitions": []},
    )

    mock_doc = MagicMock()
    mock_doc.__enter__.return_value = mock_doc
    mock_doc.__len__.return_value = 1
    processor = PdfOcrJobProcessor(
        gemini_api_key="secret-api-key",
    )

    with patch("conversion.pdf.ocr_processor.open_pdf_document", return_value=mock_doc), \
         patch(
             "conversion.gemini.query_gemini_structure",
             side_effect=gemini_response if isinstance(gemini_response, Exception) else None,
             return_value=gemini_response if not isinstance(gemini_response, Exception) else None,
         ), \
         patch("conversion.pdf.ocr_processor.generate_job_epub", return_value=tmp_path / "out.epub"), \
         patch("conversion.pdf.ocr_processor.build_quality_report", return_value=tmp_path / "report.json"):

        # Must not raise GeminiApiError; should complete smoothly using heuristic detection
        result = processor(job, workspace, store)
        assert result.next_status == JobStatus.COMPLETED

        struct = workspace.load_structure_analysis()
        assert struct["chapter_count"] == 1
        assert struct["chapters"][0]["title"] == "Chapter 1: Native Chapter"
        # Since API call failed, ai_refined is not True
        assert struct.get("ai_refined") is not True

