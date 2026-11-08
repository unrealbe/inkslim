from __future__ import annotations
import math, re, unicodedata
from collections import defaultdict
from copy import deepcopy
from typing import Any

SCHEMA_VERSION = 1
_PAGE_NUMBER = re.compile(r'^(?:[-–—]\\s*)?(?:\\d{1,4}|[ivxlcdm]{1,12})(?:\\s*[-–—])?$', re.IGNORECASE)

def _bbox(paragraph):
    value = paragraph.get('bbox')
    if not isinstance(value, (list, tuple)) or len(value) != 4: return None
    try: coords = tuple(float(item) for item in value)
    except (TypeError, ValueError): return None
    x0, y0, x1, y1 = coords
    return coords if 0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1 else None

def _normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split()).strip(' \\t\\n\\r.,:;|')

def detect_repeated_marginalia(pages: dict[int, list[dict[str, Any]]], *, total_pages: int):
    if total_pages < 1: raise ValueError('total_pages must be at least 1')
    if any(page < 1 or page > total_pages for page in pages): raise ValueError('page number is outside the document')
    threshold = max(3, math.ceil(total_pages * 0.20))
    repeated, numbers = defaultdict(list), defaultdict(list)
    for page_number, paragraphs in pages.items():
        for index, paragraph in enumerate(paragraphs):
            text, box = paragraph.get('text'), _bbox(paragraph)
            if not isinstance(text, str) or not text.strip() or box is None or len(text) > 120: continue
            y = (box[1] + box[3]) / 2
            region = 'header' if y <= 0.12 else 'footer' if y >= 0.88 else None
            key = _normalize(text)
            if region is None or not key: continue
            item = {'page_number': page_number, 'paragraph_index': index, 'text': text, 'center_x': (box[0]+box[2])/2, 'center_y': y}
            if _PAGE_NUMBER.fullmatch(key): numbers[region].append(item)
            else: repeated[(region, key)].append(item)
    removed = defaultdict(set)
    detected = []
    for (region, key), items in repeated.items():
        page_ids = sorted({x['page_number'] for x in items})
        if len(page_ids) < threshold: continue
        detected.append({'kind': region, 'normalized_text': key, 'text': items[0]['text'], 'page_numbers': page_ids, 'occurrence_count': len(items), 'reason': 'same_normalized_text_repeated_in_page_margin'})
        for item in items: removed[item['page_number']].add(item['paragraph_index'])
    for region, items in numbers.items():
        page_ids = sorted({x['page_number'] for x in items})
        if len(page_ids) < 3: continue
        xs, ys = [x['center_x'] for x in items], [x['center_y'] for x in items]
        if max(xs)-min(xs) > 0.10 or max(ys)-min(ys) > 0.035: continue
        detected.append({'kind': 'page_number', 'region': region, 'page_numbers': page_ids, 'occurrence_count': len(items), 'reason': 'varying_numeric_or_roman_marker_at_consistent_margin_position'})
        for item in items: removed[item['page_number']].add(item['paragraph_index'])
    cleaned = {page: [deepcopy(p) for i,p in enumerate(pages.get(page, [])) if i not in removed.get(page,set())] for page in range(1,total_pages+1)}
    analysis = {'schema_version': SCHEMA_VERSION, 'total_pages': total_pages, 'minimum_repetitions': threshold, 'items': detected, 'removed_paragraphs': sum(map(len, removed.values())), 'pages_with_removals': sorted(p for p,v in removed.items() if v), 'removed': [{'page_number':p,'paragraph_index':i} for p in sorted(removed) for i in sorted(removed[p])]}
    return analysis, cleaned
