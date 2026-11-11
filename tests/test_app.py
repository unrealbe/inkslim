import base64
import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

IM = shutil.which("magick") or shutil.which("convert")
pytestmark = pytest.mark.skipif(
    not (IM and shutil.which("zip") and shutil.which("unzip")),
    reason="needs imagemagick, zip and unzip",
)


def make_epub(path, images=()):
    """Build a minimal, correctly structured EPUB for integration tests."""
    src = path.parent / (path.stem + "-src")
    (src / "META-INF").mkdir(parents=True)
    (src / "mimetype").write_bytes(b"application/epub+zip")
    (src / "META-INF" / "container.xml").write_text(
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="c.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    (src / "my chapter.xhtml").write_text("<html/>")
    for name, blob in images:
        target = src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    subprocess.run(["zip", "-X0", "-q", str(path), "mimetype"], cwd=src, check=True)
    subprocess.run(["zip", "-Xr9D", "-q", str(path), ".", "-x", "mimetype"], cwd=src, check=True)
    return path


def make_image(tmp_path, name, size, extra=()):
    out = tmp_path / name
    subprocess.run(
        [IM, "-size", size, "plasma:fractal", *extra, str(out)],
        check=True,
        capture_output=True,
    )
    return out.read_bytes()


@pytest.fixture()
def big_epub(tmp_path):
    jpg = make_image(tmp_path, "photo.JPG", "2400x3200", ["-quality", "95"])
    png = make_image(tmp_path, "art.png", "1800x2400")
    return make_epub(
        tmp_path / "book.epub",
        [("img/Big Photo.JPG", jpg), ("img/art.png", png)],
    )


@pytest.fixture()
def client():
    app.AUTH_USER = app.AUTH_PASS = ""
    return app.app.test_client()


def post(client, content, filename="book.epub", **form):
    data = {"file": (io.BytesIO(content), filename), **form}
    return client.post("/shrink", data=data, content_type="multipart/form-data")


def test_index_and_health(client):
    page = client.get("/")
    assert page.status_code == 200
    assert b"Select or drop files" in page.data
    assert client.get("/healthz").data == b"ok"


def test_shrink_returns_smaller_valid_epub(client, big_epub):
    original = big_epub.read_bytes()
    response = post(client, original, "My Book, part 1.epub", gray="1")
    assert response.status_code == 200
    assert len(response.data) < len(original) / 2
    assert int(response.headers["X-Original-Size"]) == len(original)
    assert int(response.headers["X-New-Size"]) == len(response.data)
    assert "My Book, part 1-small.epub" in response.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.testzip() is None
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        assert "img/Big Photo.JPG" in archive.namelist()


def test_images_are_never_enlarged(client, tmp_path):
    small = make_image(tmp_path, "small.jpg", "300x300", ["-quality", "100"])
    epub = make_epub(tmp_path / "small.epub", [("small.jpg", small)])
    response = post(client, epub.read_bytes(), "small.epub", min_kb="1")
    if response.status_code == 200:
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            output_image = archive.read("small.jpg")
        dimensions = subprocess.run(
            [IM, "jpg:-", "-format", "%wx%h", "info:"],
            input=output_image,
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert dimensions == "300x300"
    else:
        assert response.status_code == 422


def test_rejects_non_epub_or_pdf_extension(client):
    response = post(client, b"hello", "notes.txt")
    assert response.status_code == 400


def test_rejects_non_zip_epub_content(client):
    response = post(client, b"this is not a zip", "fake.epub")
    assert response.status_code == 422


def test_no_gain_is_reported_not_crashed(client, tmp_path):
    epub = make_epub(tmp_path / "text-only.epub")
    response = post(client, epub.read_bytes(), "text-only.epub")
    assert response.status_code == 422
    assert "No size reduction" in response.get_json()["error"]


def test_weird_parameters_are_clamped(client, big_epub):
    response = post(
        client,
        big_epub.read_bytes(),
        width="999999999",
        quality="abc",
    )
    assert response.status_code == 200


def test_basic_auth(client):
    app.AUTH_USER, app.AUTH_PASS = "me", "secret"
    try:
        assert client.get("/").status_code == 401
        assert client.get("/healthz").status_code == 200
        good = {"Authorization": "Basic " + base64.b64encode(b"me:secret").decode()}
        bad = {"Authorization": "Basic " + base64.b64encode(b"me:wrong").decode()}
        assert client.get("/", headers=good).status_code == 200
        assert client.get("/", headers=bad).status_code == 401
    finally:
        app.AUTH_USER = app.AUTH_PASS = ""


def test_cli_dry_run_does_not_write_anything(tmp_path, big_epub):
    before = sorted(path.name for path in tmp_path.iterdir())
    result = subprocess.run(
        ["bash", str(ROOT / "epub-shrink.sh"), "-n", str(big_epub)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "image(s)" in result.stdout
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_cli_exit_codes(tmp_path, big_epub):
    """The web app relies on these exit codes, so they are part of the script's contract."""
    script = ["bash", str(ROOT / "epub-shrink.sh"), "-o", str(tmp_path / "out")]

    assert subprocess.run(script + [str(big_epub)], capture_output=True).returncode == 0

    text_only = make_epub(tmp_path / "text-only.epub")
    assert subprocess.run(script + [str(text_only)], capture_output=True).returncode == 2  # no gain

    fake = tmp_path / "fake.epub"
    fake.write_bytes(b"this is not a zip")
    assert subprocess.run(script + [str(fake)], capture_output=True).returncode == 3  # invalid EPUB

    missing = tmp_path / "missing.epub"
    assert subprocess.run(script + [str(missing)], capture_output=True).returncode == 5  # not found


# ============================================================================
# Security regression tests
# ============================================================================

@pytest.fixture()
def client_with_security():
    """Client with authentication disabled for response-header checks."""
    app.AUTH_USER = app.AUTH_PASS = ""
    return app.app.test_client()


def test_security_headers_present(client_with_security):
    response = client_with_security.get("/")
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert response.headers.get("Permissions-Policy") == (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    csp = response.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_security_headers_on_healthz(client_with_security):
    response = client_with_security.get("/healthz")
    assert response.status_code == 200
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


def make_zip_with_entries(tmp_path, entries, name="test.epub"):
    archive_path = tmp_path / name
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
        for entry_name, content in entries:
            archive.writestr(entry_name, content)
    return archive_path


def make_minimal_epub(tmp_path, name="minimal.epub"):
    return make_zip_with_entries(
        tmp_path,
        [
            ("mimetype", b"application/epub+zip"),
            ("META-INF/container.xml", b"<container/>"),
        ],
        name,
    )


def test_rejects_epub_with_too_many_files(client, tmp_path):
    entries = [("mimetype", b"application/epub+zip")]
    entries.extend((f"file_{i}.txt", b"x") for i in range(10000))
    archive_path = make_zip_with_entries(tmp_path, entries, "many.epub")
    response = post(client, archive_path.read_bytes(), "many.epub")
    assert response.status_code == 422
    assert "too many files" in response.get_json()["error"].lower()


@pytest.mark.parametrize("bad_name", ["../evil.txt", "/absolute.txt", r"folder\\evil.txt", "C:/evil.txt"])
def test_rejects_epub_with_invalid_paths(client, tmp_path, bad_name):
    archive_path = make_zip_with_entries(
        tmp_path,
        [("mimetype", b"application/epub+zip"), (bad_name, b"bad path")],
    )
    response = post(client, archive_path.read_bytes(), "invalid.epub")
    assert response.status_code == 422
    assert "invalid paths" in response.get_json()["error"].lower()


def test_rejects_epub_with_symlink(client, tmp_path):
    archive_path = tmp_path / "symlink.epub"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("mimetype", b"application/epub+zip")
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, b"target")
    response = post(client, archive_path.read_bytes(), "symlink.epub")
    assert response.status_code == 422
    assert "symlinks" in response.get_json()["error"].lower()


def test_rejects_epub_over_uncompressed_size_limit(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MAX_EXTRACTED_MB", 1)
    archive_path = make_zip_with_entries(
        tmp_path,
        [
            ("mimetype", b"application/epub+zip"),
            ("large.bin", b"x" * (2 * 1024 * 1024)),
        ],
        "large.epub",
    )
    response = post(client, archive_path.read_bytes(), "large.epub")
    assert response.status_code == 422
    assert "uncompressed size" in response.get_json()["error"].lower()


def test_rejects_epub_with_missing_or_wrong_mimetype(client, tmp_path):
    missing = make_zip_with_entries(tmp_path, [("chapter.xhtml", b"<html/>")])
    response = post(client, missing.read_bytes(), "missing.epub")
    assert response.status_code == 422
    assert "mimetype" in response.get_json()["error"].lower()

    wrong = make_zip_with_entries(tmp_path, [("mimetype", b"not-an-epub")], "wrong.epub")
    response = post(client, wrong.read_bytes(), "wrong.epub")
    assert response.status_code == 422
    assert "mimetype" in response.get_json()["error"].lower()


def test_accepts_valid_epub_with_security_checks(client, big_epub):
    response = post(client, big_epub.read_bytes(), "valid.epub", gray="1")
    assert response.status_code == 200
    assert len(response.data) < len(big_epub.read_bytes())


def test_rejects_pdf_over_page_limit(client, tmp_path, monkeypatch):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "many_pages.pdf"
    pdf_canvas = canvas.Canvas(str(pdf_path), pagesize=letter)
    for page_number in range(6):
        pdf_canvas.drawString(100, 700, f"Page {page_number + 1}")
        pdf_canvas.showPage()
    pdf_canvas.save()

    monkeypatch.setattr(app, "MAX_PDF_PAGES", 5)
    response = post(client, pdf_path.read_bytes(), "many_pages.pdf")
    assert response.status_code == 422
    assert "maximum page limit of 5" in response.get_json()["error"].lower()


def test_pdf_within_page_limit_passes_page_count_validation(client, tmp_path, monkeypatch):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "few_pages.pdf"
    pdf_canvas = canvas.Canvas(str(pdf_path), pagesize=letter)
    for page_number in range(3):
        pdf_canvas.drawString(100, 700, f"Page {page_number + 1}")
        pdf_canvas.showPage()
    pdf_canvas.save()

    monkeypatch.setattr(app, "MAX_PDF_PAGES", 5)
    response = post(client, pdf_path.read_bytes(), "few_pages.pdf")
    # A text-only PDF may fail because there is no size reduction; it must not
    # be rejected by the page-count limit.
    assert response.status_code != 422 or "page limit" not in response.get_json().get("error", "").lower()
