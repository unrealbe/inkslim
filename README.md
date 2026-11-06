# Dependency-free EPUB 3 builder for Inkslim

Drop-in files for the existing repository:

- `conversion/epub/__init__.py`, `conversion/epub/builder.py`
- `tests/test_epub_builder.py`

No new requirements. Uses only the Python standard library (the tests use Pillow, which is already a dependency).

## Why not EbookLib

EbookLib 0.21 is AGPL-3.0-or-later and depends on lxml. Its copyleft terms may be incompatible with
Inkslim's MIT licensing and distribution goals, so this writer deliberately avoids that dependency.
The project likewise avoids adding PDF libraries with licensing terms that do not fit those goals.

## Usage

```python
from conversion.epub import Block, Chapter, EpubMetadata, build_epub

build_epub(
    "book.epub",
    EpubMetadata(title="Titel", language="nl", author="Auteur"),
    [Chapter("Hoofdstuk 1", [Block("p", "Eerste alinea."), Block("h2", "Tussenkop"), Block("p", "Tweede.")])],
    cover_jpeg=jpeg_bytes_or_None,
)
```

## Validation

The tests include a run of the official EPUBCheck when it is available:

```bash
EPUBCHECK_JAR=/path/to/epubcheck.jar pytest tests/test_epub_builder.py
```

The builder has been validated with EPUBCheck 5.4.0: 0 fatals, 0 errors and 0 warnings, both with and without a cover.

For CI (GitHub's runners already have Java), add before the test step:

```yaml
      - name: Download EPUBCheck
        run: |
          curl -sSL -o epubcheck.zip https://github.com/w3c/epubcheck/releases/download/v5.4.0/epubcheck-5.4.0.zip
          unzip -q epubcheck.zip
          echo "EPUBCHECK_JAR=$PWD/epubcheck-5.4.0/epubcheck.jar" >> "$GITHUB_ENV"
```
