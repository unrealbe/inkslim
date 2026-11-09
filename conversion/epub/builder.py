"""Minimal EPUB 3 writer that uses only the Python standard library.

Why not EbookLib? EbookLib is AGPL-3.0-or-later and pulls in lxml. Inkslim is MIT and ships as
a Docker image, so bundling an AGPL library would put the whole distributed image under the
AGPL. An EPUB is just a zip of a few XML files; the writer below is small, has no dependencies
and is validated against the official EPUBCheck in the tests.

Produces a reflowable EPUB 3 with a navigation document, an NCX for older readers, a minimal
stylesheet and an optional cover image. Text is escaped and stripped of characters that are not
allowed in XML (OCR output regularly contains form feeds and other control characters).
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

# Characters that XML 1.0 forbids (C0 controls except tab/newline/CR, surrogates, U+FFFE/U+FFFF).
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_BLOCK_TAGS = {"p": "p", "h2": "h2", "h3": "h3"}

_CSS = """\
body { line-height: 1.4; }
h1, h2, h3 { text-align: left; page-break-after: avoid; break-after: avoid; }
h1 { margin: 1.5em 0 1em; }
p { margin: 0; text-indent: 1.2em; }
h1 + p, h2 + p, h3 + p { text-indent: 0; }
"""


class EpubBuildError(ValueError):
    """The book description is not valid enough to build an EPUB."""


@dataclass(frozen=True, slots=True)
class Block:
    """One content block. kind is 'p' (paragraph), 'h2' or 'h3' (headings inside a chapter)."""

    kind: str
    text: str
    source_page: int | None = None
    paragraph_index: int | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass(slots=True)
class Chapter:
    title: str
    blocks: list[Block] = field(default_factory=list)


@dataclass(slots=True)
class EpubMetadata:
    title: str
    language: str = "en"
    author: str | None = None
    identifier: str | None = None  # defaults to a random urn:uuid
    toc_title: str = "Contents"


def clean_text(value: str) -> str:
    """Remove characters that cannot appear in XML and normalise whitespace runs."""
    value = _XML_ILLEGAL.sub("", value)
    return re.sub(r"[ \t\u00a0]+", " ", value).strip()


def _text(value: str) -> str:
    return escape(clean_text(value))


def _attr(value: str) -> str:
    return quoteattr(clean_text(value))


def _chapter_xhtml(chapter: Chapter, language: str) -> str:
    body: list[str] = [f"    <h1>{_text(chapter.title)}</h1>"]
    for block in chapter.blocks:
        tag = _BLOCK_TAGS.get(block.kind)
        if tag is None:
            raise EpubBuildError(f"Unknown block kind: {block.kind!r}")
        text = _text(block.text)
        if text:
            attributes = ""
            if (
                block.source_page is not None
                and block.paragraph_index is not None
                and block.bbox is not None
            ):
                bbox = ",".join(f"{value:.6f}" for value in block.bbox)
                attributes = (
                    f' id="p-{block.source_page}-{block.paragraph_index}"'
                    f' data-source-page="{block.source_page}"'
                    f' data-source-bbox="{bbox}"'
                )
            body.append(f"    <{tag}{attributes}>{text}</{tag}>")
    lang = _attr(language)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" lang={lang} xml:lang={lang}>\n'
        '<head>\n  <meta charset="utf-8"/>\n'
        f"  <title>{_text(chapter.title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="styles.css"/>\n</head>\n'
        '<body>\n  <section epub:type="chapter" role="doc-chapter">\n'
        + "\n".join(body)
        + "\n  </section>\n</body>\n</html>\n"
    )


def _nav_xhtml(metadata: EpubMetadata, files: list[tuple[str, Chapter]]) -> str:
    lang = _attr(metadata.language)
    items = "\n".join(
        f'      <li><a href="{name}">{_text(chapter.title)}</a></li>' for name, chapter in files
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" lang={lang} xml:lang={lang}>\n'
        f"<head>\n  <meta charset=\"utf-8\"/>\n  <title>{_text(metadata.toc_title)}</title>\n</head>\n"
        '<body>\n  <nav epub:type="toc" id="toc" role="doc-toc">\n'
        f"    <h1>{_text(metadata.toc_title)}</h1>\n    <ol>\n{items}\n    </ol>\n  </nav>\n"
        "</body>\n</html>\n"
    )


def _ncx(metadata: EpubMetadata, identifier: str, files: list[tuple[str, Chapter]]) -> str:
    points = "\n".join(
        f'    <navPoint id="np{index}" playOrder="{index}">\n'
        f"      <navLabel><text>{_text(chapter.title)}</text></navLabel>\n"
        f'      <content src="{name}"/>\n    </navPoint>'
        for index, (name, chapter) in enumerate(files, start=1)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'  <head>\n    <meta name="dtb:uid" content={_attr(identifier)}/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        '    <meta name="dtb:totalPageCount" content="0"/>\n'
        '    <meta name="dtb:maxPageNumber" content="0"/>\n  </head>\n'
        f"  <docTitle><text>{_text(metadata.title)}</text></docTitle>\n"
        f"  <navMap>\n{points}\n  </navMap>\n</ncx>\n"
    )


def _opf(
    metadata: EpubMetadata,
    identifier: str,
    files: list[tuple[str, Chapter]],
    *,
    has_cover: bool,
    modified: datetime,
) -> str:
    lang = _attr(metadata.language)
    creator = f"    <dc:creator>{_text(metadata.author)}</dc:creator>\n" if metadata.author else ""
    cover_meta = '    <meta name="cover" content="cover-image"/>\n' if has_cover else ""
    cover_item = (
        '    <item id="cover-image" href="cover.jpg" media-type="image/jpeg" '
        'properties="cover-image"/>\n'
        if has_cover
        else ""
    )
    manifest = "\n".join(
        f'    <item id="chapter{index}" href="{name}" media-type="application/xhtml+xml"/>'
        for index, (name, _) in enumerate(files, start=1)
    )
    spine = "\n".join(
        f'    <itemref idref="chapter{index}"/>' for index in range(1, len(files) + 1)
    )
    stamp = modified.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        f'unique-identifier="book-id" xml:lang={lang}>\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="book-id">{_text(identifier)}</dc:identifier>\n'
        f"    <dc:title>{_text(metadata.title)}</dc:title>\n"
        f"    <dc:language>{_text(metadata.language)}</dc:language>\n"
        f"{creator}"
        f'    <meta property="dcterms:modified">{stamp}</meta>\n{cover_meta}'
        "  </metadata>\n  <manifest>\n"
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="css" href="styles.css" media-type="text/css"/>\n'
        f"{cover_item}{manifest}\n  </manifest>\n"
        f'  <spine toc="ncx">\n{spine}\n  </spine>\n</package>\n'
    )


_CONTAINER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '  <rootfiles>\n    <rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/>\n  </rootfiles>\n</container>\n'
)


def build_epub(
    destination: Path | str,
    metadata: EpubMetadata,
    chapters: list[Chapter],
    *,
    cover_jpeg: bytes | None = None,
    modified: datetime | None = None,
) -> Path:
    """Write an EPUB 3 file atomically and return its path."""
    if not clean_text(metadata.title):
        raise EpubBuildError("A title is required")
    if not _LANGUAGE.fullmatch(metadata.language or ""):
        raise EpubBuildError(f"Invalid language tag: {metadata.language!r}")
    if not chapters:
        raise EpubBuildError("At least one chapter is required")
    if cover_jpeg is not None and not cover_jpeg.startswith(b"\xff\xd8"):
        raise EpubBuildError("The cover must be a JPEG image")

    destination = Path(destination)
    identifier = metadata.identifier or f"urn:uuid:{uuid.uuid4()}"
    modified = modified or datetime.now(timezone.utc)
    files = [(f"chapter_{index:04d}.xhtml", chapter) for index, chapter in enumerate(chapters, 1)]

    entries: list[tuple[str, bytes]] = [
        ("META-INF/container.xml", _CONTAINER.encode("utf-8")),
        ("OEBPS/content.opf", _opf(metadata, identifier, files,
                                    has_cover=cover_jpeg is not None, modified=modified).encode("utf-8")),
        ("OEBPS/nav.xhtml", _nav_xhtml(metadata, files).encode("utf-8")),
        ("OEBPS/toc.ncx", _ncx(metadata, identifier, files).encode("utf-8")),
        ("OEBPS/styles.css", _CSS.encode("utf-8")),
    ]
    if cover_jpeg is not None:
        entries.append(("OEBPS/cover.jpg", cover_jpeg))
    entries += [
        (f"OEBPS/{name}", _chapter_xhtml(chapter, metadata.language).encode("utf-8"))
        for name, chapter in files
    ]

    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            # The EPUB container rules: 'mimetype' first, stored (not compressed), no extra field.
            first = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
            first.compress_type = zipfile.ZIP_STORED
            first.external_attr = 0o644 << 16
            archive.writestr(first, b"application/epub+zip")
            for name, data in entries:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = (
                    zipfile.ZIP_STORED if name.endswith(".jpg") else zipfile.ZIP_DEFLATED
                )
                info.external_attr = 0o644 << 16
                archive.writestr(info, data, compresslevel=9)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
