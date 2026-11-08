import zipfile
from datetime import datetime, timezone
from pathlib import Path

from conversion.epub import Block, Chapter, EpubMetadata, build_epub
from conversion.epub.validation import validate_epub


def _build(path: Path, *, text="Een geldige alinea.", chapter_title="Hoofdstuk 1") -> Path:
    return build_epub(
        path,
        EpubMetadata(title="Validatieboek", language="nl"),
        [Chapter(chapter_title, [Block("p", text)])],
        modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _rewrite(source: Path, destination: Path, *, replacements=None, omit=(), append=()):
    replacements = replacements or {}
    with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(destination, "w") as output:
        for info in original.infolist():
            if info.filename in omit:
                continue
            output.writestr(info, replacements.get(info.filename, original.read(info.filename)))
        for name, data in append:
            output.writestr(name, data)
    return destination


def test_valid_generated_epub_passes_runtime_validation(tmp_path):
    assert validate_epub(_build(tmp_path / "valid.epub")) == ()


def test_accepts_chapter_headings_without_body_paragraphs(tmp_path):
    assert validate_epub(_build(
        tmp_path / "headings-only.epub", text="   ", chapter_title="CHAPTER 1"
    )) == ()


def test_rejects_missing_manifest_resource(tmp_path):
    source = _build(tmp_path / "source.epub")
    broken = _rewrite(source, tmp_path / "missing-resource.epub", omit={"OEBPS/chapter_0001.xhtml"})
    errors = validate_epub(broken)
    assert errors and "manifest resource" in errors[0]


def test_rejects_unknown_spine_idref(tmp_path):
    source = _build(tmp_path / "source.epub")
    with zipfile.ZipFile(source) as archive:
        opf = archive.read("OEBPS/content.opf").decode()
    opf = opf.replace('idref="chapter1"', 'idref="missing-chapter"')
    broken = _rewrite(source, tmp_path / "bad-spine.epub",
                      replacements={"OEBPS/content.opf": opf.encode()})
    errors = validate_epub(broken)
    assert errors and "unknown manifest id" in errors[0]


def test_rejects_broken_navigation_fragment(tmp_path):
    source = _build(tmp_path / "source.epub")
    with zipfile.ZipFile(source) as archive:
        nav = archive.read("OEBPS/nav.xhtml").decode()
    nav = nav.replace("chapter_0001.xhtml", "chapter_0001.xhtml#missing-anchor")
    broken = _rewrite(source, tmp_path / "bad-nav.epub",
                      replacements={"OEBPS/nav.xhtml": nav.encode()})
    errors = validate_epub(broken)
    assert errors and "Missing fragment" in errors[0]


def test_rejects_malformed_spine_xhtml(tmp_path):
    source = _build(tmp_path / "source.epub")
    broken = _rewrite(source, tmp_path / "malformed.epub",
                      replacements={"OEBPS/chapter_0001.xhtml": b"<html><body><p>broken"})
    errors = validate_epub(broken)
    assert errors and "Invalid XML" in errors[0]


def test_rejects_book_without_readable_body_text(tmp_path):
    errors = validate_epub(_build(
        tmp_path / "empty-book.epub", text="   ", chapter_title="   "
    ))
    assert errors and "no readable text" in errors[0]


def test_rejects_duplicate_and_unsafe_zip_entries(tmp_path):
    source = _build(tmp_path / "source.epub")
    duplicate = _rewrite(source, tmp_path / "duplicate.epub",
                         append=[("OEBPS/chapter_0001.xhtml", b"duplicate")])
    errors = validate_epub(duplicate)
    assert errors and "Duplicate ZIP" in errors[0]

    unsafe = _rewrite(source, tmp_path / "unsafe.epub",
                      append=[("../outside.txt", b"not safe")])
    errors = validate_epub(unsafe)
    assert errors and "Unsafe ZIP" in errors[0]
