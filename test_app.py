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

import app as webapp  # noqa: E402

IM = shutil.which("magick") or shutil.which("convert")
pytestmark = pytest.mark.skipif(
    not (IM and shutil.which("zip") and shutil.which("unzip")),
    reason="needs imagemagick, zip and unzip",
)


def make_epub(path, images=()):
    """Build a minimal, structurally correct EPUB the same way the script packs one
    (mimetype first and stored, everything else deflated), so sizes are comparable."""
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
    subprocess.run([IM, "-size", size, "plasma:fractal", *extra, str(out)], check=True,
                   capture_output=True)
    return out.read_bytes()


@pytest.fixture()
def big_epub(tmp_path):
    jpg = make_image(tmp_path, "photo.JPG", "2400x3200", ["-quality", "95"])
    png = make_image(tmp_path, "art.png", "1800x2400")
    return make_epub(tmp_path / "book.epub", [("img/Big Photo.JPG", jpg), ("img/art.png", png)])


@pytest.fixture()
def client():
    webapp.AUTH_USER = webapp.AUTH_PASS = ""
    return webapp.app.test_client()


def post(client, content, filename="book.epub", **form):
    data = {"file": (io.BytesIO(content), filename), **form}
    return client.post("/shrink", data=data, content_type="multipart/form-data")


def test_index_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").data == b"ok"


def test_shrink_returns_smaller_valid_epub(client, big_epub):
    original = big_epub.read_bytes()
    r = post(client, original, "Mijn Boek, deel 1.epub", gray="1")
    assert r.status_code == 200
    assert len(r.data) < len(original) / 2
    assert int(r.headers["X-Original-Size"]) == len(original)
    assert int(r.headers["X-New-Size"]) == len(r.data)
    assert "Mijn Boek, deel 1-small.epub" in r.headers["Content-Disposition"]

    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        assert z.testzip() is None
        first = z.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        assert "img/Big Photo.JPG" in z.namelist()  # same names, so references keep working


def test_images_are_never_enlarged(client, tmp_path):
    small = make_image(tmp_path, "small.jpg", "300x300", ["-quality", "100"])
    epub = make_epub(tmp_path / "small.epub", [("small.jpg", small)])
    r = post(client, epub.read_bytes(), "small.epub", min_kb="1")
    if r.status_code == 200:
        out = zipfile.ZipFile(io.BytesIO(r.data)).read("small.jpg")
        dims = subprocess.run([IM, "jpg:-", "-format", "%wx%h", "info:"], input=out,
                              capture_output=True).stdout.decode()
        assert dims == "300x300"
    else:
        assert r.status_code == 422


def test_rejects_non_epub_extension(client):
    r = post(client, b"hello", "notes.txt")
    assert r.status_code == 400


def test_rejects_non_zip_content(client):
    r = post(client, b"this is not a zip", "fake.epub")
    assert r.status_code == 422


def test_no_gain_is_reported_not_crashed(client, tmp_path):
    epub = make_epub(tmp_path / "text-only.epub")
    r = post(client, epub.read_bytes(), "text-only.epub")
    assert r.status_code == 422
    assert "Geen winst" in r.get_json()["error"]


def test_weird_parameters_are_clamped(client, big_epub):
    r = post(client, big_epub.read_bytes(), width="999999999", quality="abc")
    assert r.status_code == 200


def test_basic_auth(client):
    webapp.AUTH_USER, webapp.AUTH_PASS = "me", "secret"
    try:
        assert client.get("/").status_code == 401
        assert client.get("/healthz").status_code == 200
        good = {"Authorization": "Basic " + base64.b64encode(b"me:secret").decode()}
        bad = {"Authorization": "Basic " + base64.b64encode(b"me:wrong").decode()}
        assert client.get("/", headers=good).status_code == 200
        assert client.get("/", headers=bad).status_code == 401
    finally:
        webapp.AUTH_USER = webapp.AUTH_PASS = ""


def test_cli_dry_run_does_not_write_anything(tmp_path, big_epub):
    before = sorted(p.name for p in tmp_path.iterdir())
    out = subprocess.run(["bash", str(ROOT / "epub-shrink.sh"), "-n", str(big_epub)],
                         capture_output=True, text=True)
    assert out.returncode == 0
    assert "afbeelding" in out.stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == before
