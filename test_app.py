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
    assert b"Drop your EPUB or PDF files here" in page.data
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
