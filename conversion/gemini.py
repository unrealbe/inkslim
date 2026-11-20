"""Google Gemini API integration for document structure refinement.

Opt-in AI assistance for heading classification and chapter boundary detection.
All local rule-based processing remains fully functional offline when no API
key is configured or if network calls fail.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(Exception):
    """Base error for Gemini API integration."""


class GeminiApiError(GeminiError):
    """Raised when the Gemini REST API returns an error or fails to respond."""


def extract_gemini_json_payload(response_data: dict[str, Any]) -> dict[str, Any]:
    """Extract and parse the JSON payload from a Gemini generateContent response."""
    candidates = response_data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise GeminiApiError("Gemini response contained no candidate outputs")

    first_candidate = candidates[0]
    content = first_candidate.get("content")
    if not isinstance(content, dict):
        raise GeminiApiError("Candidate output has no content object")

    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        raise GeminiApiError("Candidate content has no parts")

    text = parts[0].get("text")
    if not isinstance(text, str) or not text.strip():
        raise GeminiApiError("Candidate text part is empty")

    cleaned_text = text.strip()
    if cleaned_text.startswith("```"):
        cleaned_text = re.sub(r"^```(?:json)?\s*", "", cleaned_text)
        cleaned_text = re.sub(r"\s*```$", "", cleaned_text)

    try:
        parsed = json.loads(cleaned_text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GeminiApiError(f"Gemini output is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GeminiApiError("Gemini JSON output must be a JSON object")

    return parsed


def call_gemini_generate_content(
    prompt: str,
    *,
    api_key: str,
    model: str = DEFAULT_GEMINI_MODEL,
    timeout_seconds: int = 30,
    base_url: str = DEFAULT_GEMINI_BASE_URL,
) -> dict[str, Any]:
    """Call Gemini generateContent endpoint requesting a structured JSON response."""
    if not api_key or not api_key.strip():
        raise ValueError("api_key cannot be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    url = f"{base_url.rstrip('/')}/models/{model}:generateContent?key={api_key.strip()}"
    request_body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }

    raw_data = json.dumps(request_body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            if status != 200:
                raise GeminiApiError(f"Gemini API returned HTTP status {status}: {body[:200]}")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        raise GeminiApiError(
            f"Gemini HTTP {exc.code} error: {exc.reason} - {error_body[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise GeminiApiError(f"Failed to connect to Gemini API: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise GeminiApiError(f"Gemini API request timed out or network error: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GeminiApiError(f"Gemini API returned malformed response JSON: {exc}") from exc


def extract_structure_candidates(
    stitched_pages: dict[int, list[dict[str, Any]]],
    structure_analysis: dict[str, Any],
    page_annotations: dict[int, list[dict[str, Any]]],
    total_pages: int,
    *,
    max_candidates: int = 1200,
) -> list[dict[str, Any]]:
    """Extract candidate headings and structural page snippets for AI analysis."""
    excluded_pages = {
        page for page in structure_analysis.get("excluded_pages", [])
        if isinstance(page, int)
    }

    # Identify existing detected headings and chapters
    existing_headings: dict[tuple[int, int], dict[str, Any]] = {}
    for page_num, annots in page_annotations.items():
        for annot in annots:
            p_idx = annot.get("paragraph_index")
            if isinstance(p_idx, int):
                existing_headings[(page_num, p_idx)] = annot

    candidates: list[dict[str, Any]] = []

    for page_number in range(1, total_pages + 1):
        if page_number in excluded_pages:
            continue

        paragraphs = stitched_pages.get(page_number, [])
        is_early_page = page_number <= min(15, total_pages)

        for p_idx, paragraph in enumerate(paragraphs):
            text = " ".join(str(paragraph.get("text", "")).split()).strip()
            if not text:
                continue

            annot = existing_headings.get((page_number, p_idx))
            is_existing_chapter = bool(annot and annot.get("chapter_start"))
            is_existing_heading = bool(annot)

            # Check bounding box
            box = paragraph.get("bbox")
            y0 = box[1] if isinstance(box, (list, tuple)) and len(box) == 4 else None

            # Selection rules for candidates
            is_candidate = False
            if is_existing_heading or is_existing_chapter:
                is_candidate = True
            elif p_idx <= 2 and len(text) <= 150:
                is_candidate = True
            elif y0 is not None and y0 <= 0.45 and len(text) <= 120:
                is_candidate = True
            elif is_early_page and len(text) <= 100:
                is_candidate = True

            if is_candidate:
                role = "body"
                if is_existing_chapter:
                    role = "chapter_start"
                elif is_existing_heading:
                    role = "heading"

                candidates.append({
                    "page_number": page_number,
                    "paragraph_index": p_idx,
                    "text": text[:120],
                    "role": role,
                })

    if len(candidates) > max_candidates:
        # Keep all chapter starts and headings, sample others evenly
        essential = [c for c in candidates if c["role"] != "body"]
        remaining = [c for c in candidates if c["role"] == "body"]
        allowed_remaining = max_candidates - len(essential)
        if allowed_remaining > 0 and remaining:
            step = max(1, len(remaining) // allowed_remaining)
            candidates = sorted(
                essential + remaining[::step][:allowed_remaining],
                key=lambda x: (x["page_number"], x["paragraph_index"]),
            )
        else:
            candidates = essential[:max_candidates]

    return candidates


def build_structure_refinement_prompt(
    candidates: list[dict[str, Any]],
    total_pages: int,
    existing_chapters: list[dict[str, Any]],
    *,
    title: str | None = None,
) -> str:
    """Generate the instructions and candidate data prompt for Gemini."""
    book_title = title or "Unknown Book"
    existing_summary = [
        f"Page {ch['page_number']}, p_idx {ch['paragraph_index']}: '{ch['title']}'"
        for ch in existing_chapters[:50]
    ]
    existing_str = "\n".join(existing_summary) if existing_summary else "(None detected)"

    candidate_lines = [
        f"Page {c['page_number']}, p_idx {c['paragraph_index']} [{c['role']}]: {c['text']}"
        for c in candidates
    ]
    candidates_str = "\n".join(candidate_lines)

    return f"""You are an expert digital typesetter and book editor converting a PDF book to EPUB.
Your task is to identify genuine chapter divisions and major section titles from candidate layout snippets.

Document Information:
- Working Title: {book_title}
- Total Pages: {total_pages}

Current Heuristic Chapter Detections:
{existing_str}

Candidate Headings and Page Starts:
{candidates_str}

Instructions:
1. Identify all genuine chapter starts and major sections (e.g. Inleiding, Chapter 1, Hoofdstuk 2, Epilogue).
2. Filter out false positives (e.g. noise artifacts from scans, running headers, author names, page numbers, footnotes, or sub-paragraph lines).
3. If chapters were missed by local heuristics (e.g. due to styled fonts or missing keywords), identify them from the candidates.
4. Each chapter entry MUST use an exact `page_number` and `paragraph_index` from the candidate snippets provided above.
5. Provide a clean, human-readable title for each chapter.
6. Chapters must appear in sequential order of (page_number, paragraph_index).
7. If clearly found in the front matter, suggest the canonical book title and author.
8. If the book is a single short document without distinct chapters, return an empty list for `chapters`.

Respond with ONLY a JSON object matching this schema:
{{
  "book_title": string or null,
  "book_author": string or null,
  "chapters": [
    {{
      "page_number": int,
      "paragraph_index": int,
      "title": string,
      "reason": string
    }}
  ],
  "confidence": float between 0.0 and 1.0,
  "reasoning": string
}}
"""


def query_gemini_structure(
    stitched_pages: dict[int, list[dict[str, Any]]],
    structure_analysis: dict[str, Any],
    page_annotations: dict[int, list[dict[str, Any]]],
    *,
    total_pages: int,
    api_key: str,
    model: str = DEFAULT_GEMINI_MODEL,
    timeout_seconds: int = 30,
    title: str | None = None,
    base_url: str = DEFAULT_GEMINI_BASE_URL,
) -> dict[str, Any]:
    """Query Gemini API to obtain structure and chapter refinement analysis."""
    candidates = extract_structure_candidates(
        stitched_pages, structure_analysis, page_annotations, total_pages
    )
    if not candidates:
        logger.info("No candidates extracted for Gemini structure refinement")
        return {"chapters": [], "confidence": 0.0, "reasoning": "no_candidates"}

    prompt = build_structure_refinement_prompt(
        candidates,
        total_pages,
        structure_analysis.get("chapters", []),
        title=title,
    )

    response = call_gemini_generate_content(
        prompt,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        base_url=base_url,
    )
    return extract_gemini_json_payload(response)


def apply_gemini_structure_refinement(
    stitched_pages: dict[int, list[dict[str, Any]]],
    structure_analysis: dict[str, Any],
    page_annotations: dict[int, list[dict[str, Any]]],
    gemini_result: dict[str, Any],
    *,
    total_pages: int,
    model_name: str = DEFAULT_GEMINI_MODEL,
) -> tuple[dict[str, Any], dict[int, list[dict[str, Any]]]]:
    """Validate and apply Gemini chapter refinement to structure analysis and annotations."""
    proposed_chapters = gemini_result.get("chapters")
    excluded_pages = {
        page for page in structure_analysis.get("excluded_pages", [])
        if isinstance(page, int)
    }

    valid_chapters: list[dict[str, Any]] = []
    seen_keys: set[tuple[int, int]] = set()

    if isinstance(proposed_chapters, list):
        for item in proposed_chapters:
            if not isinstance(item, dict):
                continue
            page = item.get("page_number")
            p_idx = item.get("paragraph_index")
            title = item.get("title")

            if type(page) is not int or page < 1 or page > total_pages or page in excluded_pages:
                continue

            page_paragraphs = stitched_pages.get(page, [])
            if type(p_idx) is not int or p_idx < 0 or p_idx >= len(page_paragraphs):
                continue

            if not isinstance(title, str) or not title.strip():
                continue

            clean_title = " ".join(title.split()).strip()
            key = (page, p_idx)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            valid_chapters.append({
                "page_number": page,
                "paragraph_index": p_idx,
                "title": clean_title,
                "reason": str(item.get("reason", "ai_identified")),
            })

    # Sort sequentially
    valid_chapters.sort(key=lambda x: (x["page_number"], x["paragraph_index"]))

    if not valid_chapters:
        logger.info(
            "Gemini structure refinement produced 0 valid chapters; keeping heuristic structure"
        )
        structure_analysis["ai_refined"] = False
        structure_analysis["ai_skipped_reason"] = "no_valid_chapters_returned"
        return structure_analysis, page_annotations

    # Build refined chapter list
    refined_chapters: list[dict[str, Any]] = []
    chapter_keys: dict[tuple[int, int], int] = {}
    ai_confidence = float(gemini_result.get("confidence", 0.95))
    clamped_confidence = max(0.5, min(1.0, ai_confidence))

    for idx, ch in enumerate(valid_chapters, start=1):
        refined_chapters.append({
            "chapter_index": idx,
            "page_number": ch["page_number"],
            "paragraph_index": ch["paragraph_index"],
            "title": ch["title"],
            "confidence": clamped_confidence,
            "reason": f"gemini_ai_refinement: {ch['reason']}",
        })
        chapter_keys[(ch["page_number"], ch["paragraph_index"])] = idx

    # Update page annotations
    new_page_annotations: dict[int, list[dict[str, Any]]] = {}
    for page_number in range(1, total_pages + 1):
        existing = page_annotations.get(page_number, [])
        annot_by_pidx: dict[int, dict[str, Any]] = {
            a["paragraph_index"]: dict(a)
            for a in existing
            if isinstance(a.get("paragraph_index"), int)
        }

        # Check if any new chapters start on this page
        for (ch_page, ch_pidx), ch_idx in chapter_keys.items():
            if ch_page != page_number:
                continue

            matched_ch = refined_chapters[ch_idx - 1]
            if ch_pidx in annot_by_pidx:
                annot = annot_by_pidx[ch_pidx]
                annot["chapter_start"] = True
                annot["kind"] = "chapter_heading"
                annot["chapter_index"] = ch_idx
                annot["confidence"] = clamped_confidence
                annot["reason"] = matched_ch["reason"]
            else:
                paragraphs = stitched_pages.get(page_number, [])
                p_text = paragraphs[ch_pidx].get("text", matched_ch["title"])
                p_box = paragraphs[ch_pidx].get("bbox", [0.1, 0.1, 0.9, 0.2])
                annot_by_pidx[ch_pidx] = {
                    "paragraph_index": ch_pidx,
                    "text": p_text,
                    "kind": "chapter_heading",
                    "chapter_start": True,
                    "confidence": clamped_confidence,
                    "reason": matched_ch["reason"],
                    "bbox": list(p_box) if p_box else None,
                    "chapter_index": ch_idx,
                }

        # Demote prior chapter starts that are no longer chapter boundaries
        for p_idx, annot in annot_by_pidx.items():
            if (page_number, p_idx) not in chapter_keys and annot.get("chapter_start"):
                annot["chapter_start"] = False
                annot["kind"] = "section_heading"
                annot.pop("chapter_index", None)

        sorted_annots = sorted(annot_by_pidx.values(), key=lambda a: a["paragraph_index"])
        new_page_annotations[page_number] = sorted_annots

    structure_analysis["chapters"] = refined_chapters
    structure_analysis["chapter_count"] = len(refined_chapters)
    structure_analysis["ai_refined"] = True
    structure_analysis["ai_model"] = model_name
    structure_analysis["heading_count"] = sum(len(a) for a in new_page_annotations.values())

    suggested_title = gemini_result.get("book_title")
    if isinstance(suggested_title, str) and suggested_title.strip():
        structure_analysis["suggested_title"] = " ".join(suggested_title.split()).strip()

    suggested_author = gemini_result.get("book_author")
    if isinstance(suggested_author, str) and suggested_author.strip():
        structure_analysis["suggested_author"] = " ".join(suggested_author.split()).strip()

    return structure_analysis, new_page_annotations
