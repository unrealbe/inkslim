import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from conversion.epub import Block, Chapter, EpubBuildError, EpubMetadata, build_epub

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "x": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def sample_chapters():
    return [
        Chapter("Hoofdstuk 1", [
            Block("p", "Het was een koude ochtend & Marie dacht aan <de> brief."),
            Block("h2", "Een tussenkop"),
            Block("p", "Tweede alinea met ë, ï, “aanhalingstekens” en een — streepje."),
        ]),
        Chapter("Hoofdstuk 2", [Block("p", "Nog een alinea.")]),
    ]


def build(tmp_path, **kwargs):
    metadata = EpubMetadata(title="Testboek & meer", language="nl", author="A. Schrijver")
    return build_epub(tmp_path / "book.epub", metadata, kwargs.pop("chapters", sample_chapters()), **kwargs)


def parse(archive, name):
    return ET.fromstring(archive.read(name))


def test_container_rules(tmp_path):
    path = build(tmp_path)
    with zipfile.ZipFile(path) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert first.extra == b""
        assert archive.read("mimetype") == b"application/epub+zip"
        assert archive.testzip() is None


def test_all_parts_are_wellformed_and_consistent(tmp_path):
    path = build(tmp_path, cover_jpeg=JPEG)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for name in names:
            if name.endswith((".xml", ".opf", ".xhtml", ".ncx")):
                parse(archive, name)  # raises if not well-formed

        package = parse(archive, "OEBPS/content.opf")
        manifest = {i.get("id"): i for i in package.findall("opf:manifest/opf:item", NS)}
        for item in manifest.values():
            assert "OEBPS/" + item.get("href") in names
        spine = [r.get("idref") for r in package.findall("opf:spine/opf:itemref", NS)]
        assert spine == ["chapter1", "chapter2"]
        assert all(idref in manifest for idref in spine)
        assert manifest["nav"].get("properties") == "nav"
        assert manifest["cover-image"].get("properties") == "cover-image"
        assert package.findtext("opf:metadata/dc:language", namespaces=NS) == "nl"
        assert package.findtext("opf:metadata/dc:title", namespaces=NS) == "Testboek & meer"

        nav = parse(archive, "OEBPS/nav.xhtml")
        links = [a.get("href") for a in nav.findall(".//x:nav//x:a", NS)]
        assert links == ["chapter_0001.xhtml", "chapter_0002.xhtml"]
        ncx = parse(archive, "OEBPS/toc.ncx")
        assert [p.get("playOrder") for p in ncx.findall(".//ncx:navPoint", NS)] == ["1", "2"]


def test_text_is_escaped_and_roundtrips(tmp_path):
    path = build(tmp_path)
    with zipfile.ZipFile(path) as archive:
        chapter = parse(archive, "OEBPS/chapter_0001.xhtml")
    paragraphs = [p.text for p in chapter.findall(".//x:p", NS)]
    assert paragraphs[0] == "Het was een koude ochtend & Marie dacht aan <de> brief."
    assert "ë, ï, “aanhalingstekens” en een — streepje." in paragraphs[1]
    assert chapter.find(".//x:h2", NS).text == "Een tussenkop"


def test_illegal_xml_characters_are_removed(tmp_path):
    chapters = [Chapter("Titel\x0c", [Block("p", "woord\x00 met\x0b rommel\x1f en \ufffe einde")])]
    path = build(tmp_path, chapters=chapters)
    with zipfile.ZipFile(path) as archive:
        chapter = parse(archive, "OEBPS/chapter_0001.xhtml")  # must still parse
    assert chapter.find(".//x:p", NS).text == "woord met rommel en einde"


def test_empty_blocks_are_skipped(tmp_path):
    chapters = [Chapter("Leeg", [Block("p", "   "), Block("p", "Echte tekst")])]
    with zipfile.ZipFile(build(tmp_path, chapters=chapters)) as archive:
        chapter = parse(archive, "OEBPS/chapter_0001.xhtml")
    assert [p.text for p in chapter.findall(".//x:p", NS)] == ["Echte tekst"]


@pytest.mark.parametrize("metadata, chapters, message", [
    (EpubMetadata(title="  "), [Chapter("a")], "title"),
    (EpubMetadata(title="x", language="not a language"), [Chapter("a")], "language"),
    (EpubMetadata(title="x"), [], "chapter"),
    (EpubMetadata(title="x"), [Chapter("a", [Block("script", "x")])], "block"),
])
def test_invalid_input_is_rejected(tmp_path, metadata, chapters, message):
    with pytest.raises(EpubBuildError, match=message):
        build_epub(tmp_path / "x.epub", metadata, chapters)
    assert not list(tmp_path.iterdir())  # nothing left behind


def test_cover_must_be_jpeg(tmp_path):
    with pytest.raises(EpubBuildError, match="JPEG"):
        build(tmp_path, cover_jpeg=b"not an image")


def test_failed_build_keeps_existing_file_and_leaves_no_temp_files(tmp_path):
    good = build(tmp_path)
    before = good.read_bytes()
    bad = [Chapter("x", [Block("nope", "text")])]
    with pytest.raises(EpubBuildError):
        build(tmp_path, chapters=bad)
    assert good.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["book.epub"]


def _epubcheck_command():
    java = shutil.which("java")
    jar = os.environ.get("EPUBCHECK_JAR")
    if java and jar and Path(jar).is_file():
        return [java, "-jar", jar]
    executable = shutil.which("epubcheck")
    return [executable] if executable else None


@pytest.mark.skipif(_epubcheck_command() is None, reason="EPUBCheck not available (set EPUBCHECK_JAR)")
@pytest.mark.parametrize("with_cover", [False, True])
def test_passes_official_epubcheck(tmp_path, with_cover):
    from PIL import Image
    import io

    cover = None
    if with_cover:
        buffer = io.BytesIO()
        Image.new("L", (300, 400), 200).save(buffer, "JPEG")
        cover = buffer.getvalue()
    path = build(tmp_path, cover_jpeg=cover)
    result = subprocess.run([*_epubcheck_command(), str(path)], capture_output=True, text=True, timeout=180)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "ERROR" not in output and "FATAL" not in output, output
    assert "WARNING" not in output, output



def test_paragraph_styles_separate_paragraphs_without_first_line_indent(tmp_path):
    path = build(tmp_path)
    with zipfile.ZipFile(path) as archive:
        stylesheet = archive.read("OEBPS/styles.css").decode("utf-8")
    assert "Inkslim EPUB layout version: 2" in stylesheet
    assert "p { margin: 0 0 0.8em; text-indent: 0; }" in stylesheet
    assert "text-indent: 1.2em" not in stylesheet
