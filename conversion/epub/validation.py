"""Structural validation for generated EPUB 3 archives.

This lightweight runtime guard complements (but does not replace) EPUBCheck in CI.
"""
from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_REQUIRED_XHTML = "application/xhtml+xml"
_READABLE_BLOCKS = {"p", "h1", "h2", "h3", "blockquote", "li"}


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _safe_archive_path(name: str) -> bool:
    parts = name[:-1].split("/") if name.endswith("/") else name.split("/")
    return bool(name) and not name.startswith("/") and "\\" not in name and all(
        part not in {"", ".", ".."} for part in parts
    )


def _resolve_href(base_file: str, href: str) -> tuple[str, str] | None:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        return None
    if parsed.query:
        raise ValueError("Local EPUB references must not contain query strings")
    raw_path = unquote(parsed.path)
    if "\\" in raw_path or raw_path.startswith("/"):
        raise ValueError("Unsafe absolute or backslash reference")
    target = (
        posixpath.normpath(base_file)
        if not raw_path
        else posixpath.normpath(posixpath.join(posixpath.dirname(base_file), raw_path))
    )
    if target in {"", ".", ".."} or target.startswith("../"):
        raise ValueError("EPUB reference escapes the archive root")
    return target, unquote(parsed.fragment)


def _parse_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(name))
    except (ET.ParseError, KeyError) as exc:
        raise ValueError(f"Invalid XML document: {name}") from exc


def _check_local_references(root: ET.Element, source_path: str, names: set[str],
                            archive: zipfile.ZipFile) -> None:
    for element in root.iter():
        for attribute in ("href", "src"):
            value = element.get(attribute)
            if not value:
                continue
            resolved = _resolve_href(source_path, value)
            if resolved is None:
                continue
            target, fragment = resolved
            if target not in names:
                raise ValueError(f"Missing referenced EPUB resource: {target}")
            if fragment and target.lower().endswith((".xhtml", ".html", ".xml")):
                target_root = _parse_xml(archive, target)
                identifiers = {
                    value for node in target_root.iter()
                    for value in (node.get("id"), node.get("name")) if value
                }
                if fragment not in identifiers:
                    raise ValueError(f"Missing fragment #{fragment} in {target}")


def validate_epub(path: Path | str) -> tuple[str, ...]:
    """Return structural errors; an empty tuple means the EPUB is usable."""
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size < 100:
            return ("EPUB file is missing or too small",)
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if not infos or infos[0].filename != "mimetype":
                raise ValueError("mimetype must be the first ZIP entry")
            if infos[0].compress_type != zipfile.ZIP_STORED or infos[0].extra:
                raise ValueError("mimetype must be stored without compression or extra fields")
            if archive.read("mimetype") != b"application/epub+zip":
                raise ValueError("Invalid EPUB mimetype content")
            corrupt_entry = archive.testzip()
            if corrupt_entry is not None:
                raise ValueError(f"Corrupt ZIP entry: {corrupt_entry}")

            names_list = [info.filename for info in infos]
            if len(names_list) != len(set(names_list)):
                raise ValueError("Duplicate ZIP entry names are not allowed")
            if any(not _safe_archive_path(name) for name in names_list):
                raise ValueError("Unsafe ZIP entry path")
            names = set(names_list)

            container_path = "META-INF/container.xml"
            if container_path not in names:
                raise ValueError("Missing META-INF/container.xml")
            container = _parse_xml(archive, container_path)
            rootfiles = container.findall(f".//{_tag(_CONTAINER_NS, 'rootfile')}")
            if not rootfiles:
                raise ValueError("container.xml has no rootfile")
            opf_path = rootfiles[0].get("full-path", "")
            if not opf_path or opf_path not in names or not _safe_archive_path(opf_path):
                raise ValueError("container.xml references a missing or unsafe package document")

            package = _parse_xml(archive, opf_path)
            if package.tag != _tag(_OPF_NS, "package") or package.get("version", "").split(".")[0] != "3":
                raise ValueError("The package document is not an EPUB 3 OPF document")

            manifest_node = package.find(_tag(_OPF_NS, "manifest"))
            if manifest_node is None:
                raise ValueError("Missing OPF manifest")
            manifest: dict[str, tuple[str, str, set[str]]] = {}
            for item in manifest_node.findall(_tag(_OPF_NS, "item")):
                item_id, href, media_type = item.get("id", ""), item.get("href", ""), item.get("media-type", "")
                if not item_id or item_id in manifest or not href or not media_type:
                    raise ValueError("Invalid or duplicate OPF manifest item")
                resolved = _resolve_href(opf_path, href)
                if resolved is None:
                    raise ValueError("OPF manifest items must be local resources")
                target, fragment = resolved
                if fragment or target not in names:
                    raise ValueError(f"Missing or invalid manifest resource: {href}")
                manifest[item_id] = (target, media_type, set(item.get("properties", "").split()))

            nav_items = [item for item in manifest.values() if "nav" in item[2]]
            if len(nav_items) != 1 or nav_items[0][1] != _REQUIRED_XHTML:
                raise ValueError("The OPF manifest must identify exactly one XHTML navigation document")
            nav_path = nav_items[0][0]

            spine = package.find(_tag(_OPF_NS, "spine"))
            itemrefs = spine.findall(_tag(_OPF_NS, "itemref")) if spine is not None else []
            if not itemrefs:
                raise ValueError("The OPF spine is missing or empty")
            spine_paths = []
            for itemref in itemrefs:
                idref = itemref.get("idref", "")
                if idref not in manifest:
                    raise ValueError(f"Spine references unknown manifest id: {idref}")
                target, media_type, _ = manifest[idref]
                if media_type != _REQUIRED_XHTML:
                    raise ValueError(f"Spine item is not XHTML: {idref}")
                spine_paths.append(target)

            parsed: dict[str, ET.Element] = {}
            for name in names:
                if name.lower().endswith((".xml", ".opf", ".xhtml", ".html", ".ncx")):
                    parsed[name] = _parse_xml(archive, name)

            nav = parsed.get(nav_path)
            if nav is None or nav.tag != _tag(_XHTML_NS, "html"):
                raise ValueError("Navigation resource is not valid XHTML")
            toc = nav.find(f".//{_tag(_XHTML_NS, 'nav')}")
            if toc is None or not toc.findall(f".//{_tag(_XHTML_NS, 'a')}"):
                raise ValueError("Navigation document has no table-of-contents links")

            has_readable_text = False
            for chapter_path in spine_paths:
                chapter = parsed.get(chapter_path)
                if chapter is None or chapter.tag != _tag(_XHTML_NS, "html"):
                    raise ValueError(f"Spine resource is not valid XHTML: {chapter_path}")
                body = chapter.find(_tag(_XHTML_NS, "body"))
                if body is None:
                    raise ValueError(f"Spine XHTML has no body: {chapter_path}")
                for node in body.iter():
                    if node.tag in {_tag(_XHTML_NS, name) for name in _READABLE_BLOCKS} and "".join(node.itertext()).strip():
                        has_readable_text = True
                        break
                _check_local_references(chapter, chapter_path, names, archive)
            if not has_readable_text:
                raise ValueError("The EPUB spine contains no readable text")
            _check_local_references(nav, nav_path, names, archive)
            for document_path, document in parsed.items():
                if document_path.lower().endswith((".xhtml", ".html", ".ncx")) and document_path != nav_path and document_path not in spine_paths:
                    _check_local_references(document, document_path, names, archive)
        return ()
    except (zipfile.BadZipFile, OSError, KeyError, IndexError, ValueError, RuntimeError, ET.ParseError) as exc:
        return (str(exc) or "Invalid EPUB archive",)
