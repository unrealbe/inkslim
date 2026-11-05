"""Inkslim web app for shrinking EPUB and PDF files.

EPUB processing is delegated to epub-shrink.sh.
PDF optimization uses pikepdf; e-reader rendering uses PDFium and ReportLab.
Uploads are handled in temporary directories and removed after processing.
"""

import hmac
import io
import logging
import os
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
from PIL import Image
import reportlab.rl_config as rl_config
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from flask import Flask, Response, jsonify, request, send_file

# Store page images as raw JPEG (DCTDecode). ReportLab's default ASCII85 text encoding
# makes every image about 20 percent larger for no benefit.
rl_config.useA85 = 0

app = Flask(__name__)
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
TIMEOUT = int(os.environ.get("SHRINK_TIMEOUT", "840"))
SCRIPT = os.environ.get(
    "SHRINK_SCRIPT", str(Path(__file__).with_name("epub-shrink.sh"))
)
AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
app.logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


@app.before_request
def require_login():
    """Optional password protection (set BASIC_AUTH_USER and BASIC_AUTH_PASS)."""
    if not AUTH_USER or request.path == "/healthz":
        return None

    auth = request.authorization
    if auth:
        ok_user = hmac.compare_digest(
            (auth.username or "").encode(), AUTH_USER.encode()
        )
        ok_pass = hmac.compare_digest(
            (auth.password or "").encode(), AUTH_PASS.encode()
        )
        if ok_user and ok_pass:
            return None

    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="inkslim"'},
    )


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error=f"File exceeds {MAX_MB} MB."), 413


def number(name, default, low, high):
    """Read a bounded integer from the form, using a safe default."""
    try:
        value = int(request.form.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def safe_stem(filename):
    stem = Path(os.path.basename(filename or "book")).stem
    stem = re.sub(r"[\x00-\x1f\\/:*?\"<>|]", "_", stem).strip(" .") or "book"
    return stem[:150]


def inspect_pdf(path):
    """Validate a PDF and return its page count and basic document metadata."""
    try:
        with pikepdf.open(path) as pdf:
            page_count = len(pdf.pages)
            if page_count < 1:
                raise ValueError("This PDF does not contain any pages.")
            metadata = {}
            for key in ("/Title", "/Author", "/Subject", "/Keywords", "/Creator"):
                value = pdf.docinfo.get(key)
                if value:
                    metadata[key] = str(value)
            return page_count, metadata
    except pikepdf.PasswordError as exc:
        raise ValueError(
            "Password-protected PDFs are not supported. Remove the password first."
        ) from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("This does not appear to be a valid PDF.") from exc


def pdf_page_pixel_limits(pdf, dpi):
    """Estimate a safe image-pixel ceiling from the largest page box in the PDF."""
    max_width_pt = 0.0
    max_height_pt = 0.0
    for page in pdf.pages:
        box = page.cropbox
        width_pt = abs(float(box[2]) - float(box[0]))
        height_pt = abs(float(box[3]) - float(box[1]))
        max_width_pt = max(max_width_pt, width_pt)
        max_height_pt = max(max_height_pt, height_pt)
    if max_width_pt <= 0 or max_height_pt <= 0:
        max_width_pt, max_height_pt = 612.0, 792.0
    return (
        max(1, round(max_width_pt * dpi / 72)),
        max(1, round(max_height_pt * dpi / 72)),
    )


def optimize_pdf(source, destination, dpi, quality, min_kb, grayscale):
    """Downsample large embedded images while retaining PDF text/vector content."""
    changed_images = 0
    skipped_images = 0

    with pikepdf.open(source) as pdf:
        max_width, max_height = pdf_page_pixel_limits(pdf, dpi)

        # Iterate indirect PDF objects so images referenced from nested Form XObjects
        # are included as well. Inline images and images with masks are left untouched.
        for obj in list(pdf.objects):
            try:
                if not isinstance(obj, pikepdf.Stream):
                    continue
                if obj.get("/Subtype") != pikepdf.Name("/Image"):
                    continue
                if obj.get("/ImageMask", False) or "/SMask" in obj or "/Mask" in obj:
                    skipped_images += 1
                    continue

                width = int(obj.get("/Width", 0))
                height = int(obj.get("/Height", 0))
                if width < 1 or height < 1:
                    skipped_images += 1
                    continue
                # Bilevel scans (CCITT/JBIG2) are already tiny; JPEG would only make them bigger.
                if int(obj.get("/BitsPerComponent", 8)) == 1:
                    skipped_images += 1
                    continue
                raw_bytes = obj.read_raw_bytes()
                if len(raw_bytes) < min_kb * 1024:
                    continue

                try:
                    pil_image = pikepdf.PdfImage(obj).as_pil_image()
                except Exception:
                    skipped_images += 1
                    continue

                try:
                    if "A" in pil_image.getbands() or pil_image.info.get("transparency") is not None:
                        skipped_images += 1
                        continue

                    scale = min(1.0, max_width / width, max_height / height)
                    new_size = (
                        max(1, round(width * scale)),
                        max(1, round(height * scale)),
                    )
                    if new_size != pil_image.size:
                        resized_image = pil_image.resize(new_size, Image.Resampling.LANCZOS)
                        pil_image.close()
                        pil_image = resized_image

                    if grayscale:
                        converted = pil_image.convert("L")
                        color_space = pikepdf.Name("/DeviceGray")
                    else:
                        # CMYK (and anything else) becomes RGB. Writing CMYK JPEGs back without
                        # the matching /Decode array turns them almost black in PDF viewers.
                        converted = pil_image.convert("RGB")
                        color_space = pikepdf.Name("/DeviceRGB")

                    buffer = io.BytesIO()
                    converted.save(
                        buffer,
                        format="JPEG",
                        quality=quality,
                        optimize=True,
                        progressive=True,
                    )
                    jpeg_data = buffer.getvalue()
                    converted.close()

                    # Only replace a stream when it clearly pays off (at least 10 percent smaller);
                    # this also avoids pointless generation loss on images that are already compact.
                    if len(jpeg_data) > len(raw_bytes) * 0.9:
                        skipped_images += 1
                        continue

                    obj.write(jpeg_data, filter=pikepdf.Name("/DCTDecode"))
                    obj["/Width"] = new_size[0]
                    obj["/Height"] = new_size[1]
                    obj["/ColorSpace"] = color_space
                    obj["/BitsPerComponent"] = 8
                    for key in ("/Decode", "/DecodeParms", "/SMaskInData"):
                        if key in obj:
                            del obj[key]
                    changed_images += 1
                finally:
                    pil_image.close()
            except Exception as exc:
                # One unusual image should not prevent the rest of the PDF being optimized.
                skipped_images += 1
                app.logger.debug("Skipped a PDF image during optimization: %s", exc)

        pdf.save(
            destination,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )

    if changed_images == 0 and skipped_images > 0:
        app.logger.warning(
            "No images were optimized (%d skipped). Masked, bilevel or unsupported images "
            "are left untouched.", skipped_images
        )
    app.logger.info(
        "PDF image optimization finished: images_changed=%d images_skipped=%d "
        "target_dpi=%d grayscale=%s",
        changed_images,
        skipped_images,
        dpi,
        grayscale,
    )


def render_pdf_for_ereader(source, destination, width, height, quality, grayscale, metadata):
    """Render every PDF page to an e-reader-sized JPEG and rebuild a simple PDF."""
    document = pdfium.PdfDocument(str(source))
    pdf_canvas = None

    try:
        pdf_canvas = canvas.Canvas(str(destination), pageCompression=1)
        pdf_canvas.setTitle(metadata.get("/Title", ""))
        pdf_canvas.setAuthor(metadata.get("/Author", ""))
        pdf_canvas.setSubject(metadata.get("/Subject", ""))
        pdf_canvas.setKeywords(metadata.get("/Keywords", ""))
        pdf_canvas.setCreator("Inkslim")

        for page_index in range(len(document)):
            page = document[page_index]
            try:
                page_width_pt, page_height_pt = page.get_size()
                scale = min(width / page_width_pt, height / page_height_pt)
                bitmap = page.render(
                    scale=scale,
                    grayscale=grayscale,
                    fill_color=(255, 255, 255, 255),
                    draw_annots=True,
                    may_draw_forms=True,
                )
                rendered_image = bitmap.to_pil()
                image = None
                image_buffer = io.BytesIO()
                try:
                    image = rendered_image.convert("L" if grayscale else "RGB")
                    image.save(
                        image_buffer,
                        format="JPEG",
                        quality=quality,
                        optimize=True,
                        progressive=True,
                    )
                    image_buffer.seek(0)

                    pdf_canvas.setPageSize((page_width_pt, page_height_pt))
                    pdf_canvas.drawImage(
                        ImageReader(image_buffer),
                        0,
                        0,
                        width=page_width_pt,
                        height=page_height_pt,
                    )
                    pdf_canvas.showPage()
                finally:
                    if image is not None:
                        image.close()
                    rendered_image.close()
                    image_buffer.close()
                    bitmap.close()
            finally:
                page.close()

        pdf_canvas.save()
    finally:
        document.close()


def validate_processed_pdf(path, expected_pages):
    """Check that a PDF processor produced a readable file with all pages."""
    try:
        with pikepdf.open(path) as pdf:
            if len(pdf.pages) != expected_pages:
                raise RuntimeError("The PDF processor produced an incomplete output file.")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("The PDF processor produced an invalid output file.") from exc


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.post("/shrink")
def shrink():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="No file received."), 400

    filename = upload.filename
    extension = Path(filename).suffix.lower()
    if extension not in {".epub", ".pdf"}:
        return jsonify(error="Only .epub and .pdf files are supported."), 400

    width = number("width", 768, 200, 4000)
    height = number("height", 1024, 200, 6000)
    quality = number("quality", 75, 10, 100)
    min_kb = number("min_kb", 50, 1, 10000)
    grayscale = request.form.get("gray") == "1"
    pdf_mode = request.form.get("pdf_mode", "optimize")
    if pdf_mode not in {"optimize", "ereader"}:
        pdf_mode = "optimize"
    pdf_dpi = number("pdf_dpi", 150, 72, 600)

    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="inkslim-") as tmp_dir:
        tmp = Path(tmp_dir)
        source = tmp / ("book.epub" if extension == ".epub" else "book.pdf")
        output_dir = tmp / "out"
        output_dir.mkdir()
        upload.save(source)
        original_size = source.stat().st_size

        app.logger.info(
            "Processing started: format=%s size_bytes=%d mode=%s",
            extension[1:].upper(),
            original_size,
            pdf_mode if extension == ".pdf" else "epub-shrink",
        )

        if extension == ".epub":
            if not zipfile.is_zipfile(source):
                return jsonify(error="This does not appear to be a valid EPUB."), 422

            command = [
                "bash", SCRIPT,
                "-w", str(width),
                "-h", str(height),
                "-q", str(quality),
                "-m", str(min_kb),
                "-o", str(output_dir),
            ]
            if grayscale:
                command.append("-g")
            command.append(str(source))

            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=TIMEOUT
                )
            except subprocess.TimeoutExpired:
                app.logger.error("EPUB processing timed out")
                return jsonify(error="EPUB processing took too long."), 504

            output = output_dir / "book-small.epub"
            if not output.is_file():
                if "Geen winst" in result.stdout:
                    return jsonify(
                        error="No size reduction was possible with these settings."
                    ), 422
                if "geen geldige epub" in result.stderr.lower() or "MISLUKT" in result.stderr:
                    return jsonify(error="This does not appear to be a valid EPUB."), 422
                app.logger.error("epub-shrink failed: %s", result.stderr[-1000:])
                return jsonify(error="EPUB processing failed."), 500

            mime_type = "application/epub+zip"
            output_extension = ".epub"
            processing_mode = "epub-shrink"
            page_count = None

        else:
            try:
                page_count, metadata = inspect_pdf(source)
            except ValueError as exc:
                return jsonify(error=str(exc)), 422

            output = output_dir / "book-small.pdf"
            try:
                if pdf_mode == "optimize":
                    optimize_pdf(source, output, pdf_dpi, quality, min_kb, grayscale)
                else:
                    render_pdf_for_ereader(
                        source, output, width, height, quality, grayscale, metadata
                    )
                validate_processed_pdf(output, page_count)
            except Exception as exc:
                app.logger.exception("PDF processing failed: mode=%s", pdf_mode)
                message = str(exc) or "PDF processing failed."
                return jsonify(error=message), 500

            mime_type = "application/pdf"
            output_extension = ".pdf"
            processing_mode = pdf_mode

        new_size = output.stat().st_size
        if extension == ".pdf" and new_size >= original_size:
            if pdf_mode == "ereader":
                message = ("E-reader mode would make this PDF larger (typical for text-based PDFs). "
                           "Use Optimize PDF instead.")
            else:
                message = "No size reduction was possible with these settings."
            return jsonify(error=message), 422
        data = io.BytesIO(output.read_bytes())
        elapsed = time.monotonic() - started
        saved_percent = (
            100 * (original_size - new_size) / original_size if original_size else 0
        )
        app.logger.info(
            "Processing completed: format=%s mode=%s pages=%s elapsed=%.1fs "
            "original_mb=%.2f output_mb=%.2f saved_percent=%.1f",
            extension[1:].upper(),
            processing_mode,
            page_count if page_count is not None else "n/a",
            elapsed,
            original_size / 1048576,
            new_size / 1048576,
            saved_percent,
        )

    response = send_file(
        data,
        mimetype=mime_type,
        as_attachment=True,
        download_name=f"{safe_stem(filename)}-small{output_extension}",
    )
    response.headers["X-Original-Size"] = str(original_size)
    response.headers["X-New-Size"] = str(new_size)
    return response


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#2f6f5e">
<title>Inkslim - EPUB & PDF optimizer</title>
<link rel="icon" type="image/png" sizes="192x192" href="/static/inkslim-icon-192.png">
<link rel="apple-touch-icon" href="/static/inkslim-icon-192.png">
<link rel="manifest" href="/static/site.webmanifest">
<style>
  :root {
    --bg:#f6f5f1; --card:#fff; --text:#1d1d1b; --muted:#6b6a66;
    --line:#d9d7d0; --accent:#2f6f5e; --accent-text:#fff; --drop:#eef4f1;
    --err:#b3261e;
  }
  @media (prefers-color-scheme:dark) {
    :root {
      --bg:#151615; --card:#1e201f; --text:#ecebe6; --muted:#9a9890;
      --line:#34372f; --accent:#5fb89f; --accent-text:#0e1512; --drop:#1b2924;
      --err:#f2a29b;
    }
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    padding:24px 16px 48px;
  }
  main { max-width:640px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:12px; margin:0 0 20px; }
  .brand img { width:52px; height:52px; border-radius:11px; flex:0 0 auto; }
  h1 { font-size:1.5rem; margin:0 0 2px; line-height:1.2; }
  p.sub { color:var(--muted); margin:0; }
  .card {
    background:var(--card); border:1px solid var(--line);
    border-radius:12px; padding:16px; margin-bottom:16px;
  }
  #drop {
    border:2px dashed var(--line); border-radius:12px; padding:36px 16px;
    text-align:center; cursor:pointer; transition:background .15s,border-color .15s;
  }
  #drop.over { background:var(--drop); border-color:var(--accent); }
  #drop strong { display:block; font-size:1.1rem; }
  #drop span { color:var(--muted); font-size:.9rem; }
  details summary { cursor:pointer; font-weight:600; }
  .settings-panel summary { list-style-position:inside; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }
  label { display:block; font-size:.85rem; color:var(--muted); }
  input[type=number],select {
    width:100%; margin-top:2px; padding:8px; font:inherit; color:var(--text);
    background:var(--bg); border:1px solid var(--line); border-radius:8px;
  }
  .check {
    display:flex; align-items:center; gap:8px; margin-top:12px;
    color:var(--text); font-size:1rem;
  }
  .hint { color:var(--muted); font-size:.85rem; margin-top:12px; }
  .section-heading { font-weight:600; margin:14px 0 2px; }
  .mode-fields[hidden] { display:none !important; }
  ul { list-style:none; margin:0; padding:0; }
  li { padding:12px 0; border-top:1px solid var(--line); }
  li:first-child { border-top:0; }
  .name { font-weight:600; word-break:break-word; }
  .meta { color:var(--muted); font-size:.9rem; }
  .err { color:var(--err); }
  progress { width:100%; height:8px; margin-top:6px; accent-color:var(--accent); }
  a.btn {
    display:inline-block; margin-top:6px; padding:8px 14px; border-radius:8px;
    background:var(--accent); color:var(--accent-text);
    text-decoration:none; font-weight:600;
  }
  .history-header {
    display:flex; justify-content:space-between; align-items:center;
    gap:12px; margin-bottom:8px; font-weight:600;
  }
  #clear-history {
    border:1px solid var(--line); border-radius:8px; padding:6px 10px;
    background:var(--bg); color:var(--text); font:inherit; font-size:.85rem;
    cursor:pointer;
  }
  #clear-history:hover:not(:disabled) { border-color:var(--accent); }
  #clear-history:disabled { opacity:.5; cursor:not-allowed; }
  @media (max-width:440px) { .grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<main>
  <header class="brand">
    <img src="/static/inkslim-icon-192.png" alt="" width="52" height="52">
    <div>
      <h1>Inkslim</h1>
      <p class="sub">Shrink EPUB and PDF files for your e-reader.</p>
    </div>
  </header>

  <div id="drop" class="card" tabindex="0" role="button" aria-label="Choose EPUB or PDF files">
    <strong>Drop your EPUB or PDF files here</strong>
    <span>or click to browse</span>
    <input id="file" type="file" accept=".epub,.pdf,application/epub+zip,application/pdf" multiple hidden>
  </div>

  <details class="card settings-panel" id="epub-settings" open>
    <summary>EPUB settings</summary>
    <div class="grid">
      <label style="grid-column:1 / -1">Preset
        <select id="epub_preset">
          <option value="glo">Kobo Glo / black-and-white e-ink (768 x 1024, grayscale)</option>
          <option value="color">Colour screen, tablet or phone (1200 x 1600)</option>
          <option value="custom">Custom values</option>
        </select>
      </label>
      <label>Maximum width (px)<input id="epub_width" type="number" value="768" min="200" max="4000"></label>
      <label>Maximum height (px)<input id="epub_height" type="number" value="1024" min="200" max="6000"></label>
      <label>JPEG quality (10-100)<input id="epub_quality" type="number" value="75" min="10" max="100"></label>
      <label>Only process images larger than (kB)<input id="epub_min_kb" type="number" value="50" min="1" max="10000"></label>
    </div>
    <label class="check"><input id="epub_gray" type="checkbox" checked> Grayscale (smaller; no loss on a black-and-white screen)</label>
    <p class="hint">These settings apply only to EPUB files. Images are never enlarged.</p>
  </details>

  <details class="card settings-panel" id="pdf-settings">
    <summary>PDF settings</summary>
    <div class="grid">
      <label style="grid-column:1 / -1">PDF processing mode
        <select id="pdf_mode">
          <option value="optimize">Optimize PDF (keep text selectable)</option>
          <option value="ereader">E-reader mode (convert pages to images)</option>
        </select>
      </label>
    </div>

    <div class="mode-fields" id="pdf-optimize-fields">
      <div class="section-heading">Optimize PDF</div>
      <div class="grid">
        <label>Embedded image resolution (DPI)<input id="pdf_dpi" type="number" value="150" min="72" max="600"></label>
        <label>Only process images larger than (kB)<input id="pdf_min_kb" type="number" value="50" min="1" max="10000"></label>
      </div>
    </div>

    <div class="mode-fields" id="pdf-ereader-fields" hidden>
      <div class="section-heading">E-reader page size</div>
      <div class="grid">
        <label>Maximum width (px)<input id="pdf_width" type="number" value="768" min="200" max="4000"></label>
        <label>Maximum height (px)<input id="pdf_height" type="number" value="1024" min="200" max="6000"></label>
      </div>
    </div>

    <div class="grid">
      <label>JPEG quality (10-100)<input id="pdf_quality" type="number" value="75" min="10" max="100"></label>
    </div>
    <label class="check"><input id="pdf_gray" type="checkbox"> Grayscale</label>
    <p class="hint" id="pdf-mode-hint">Optimize PDF compresses embedded images while preserving selectable text and vector graphics. Digital signatures may no longer be valid, and some interactive features may change.</p>
    <p class="hint">These settings apply only to PDF files and are independent of EPUB settings.</p>
  </details>

  <p class="hint">Your original files are never modified. Uploads are processed in temporary folders and deleted afterwards.</p>

  <div class="card" id="results" hidden>
    <div class="history-header">
      <span>History</span>
      <button id="clear-history" type="button" disabled>Clear history</button>
    </div>
    <ul id="list"></ul>
  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file"), list = $("list"), results = $("results");
const clearButton = $("clear-history");
const pdfMode = $("pdf_mode");
const pdfOptimizeFields = $("pdf-optimize-fields");
const pdfEreaderFields = $("pdf-ereader-fields");
const pdfModeHint = $("pdf-mode-hint");
const SETTINGS_KEY = "inkslim.preferences.v1";

const PRESETS = {
  glo: {width:768,height:1024,quality:75,min_kb:50,gray:true},
  color: {width:1200,height:1600,quality:80,min_kb:50,gray:false}
};
const SETTING_IDS = [
  "epub_preset","epub_width","epub_height","epub_quality","epub_min_kb","epub_gray",
  "pdf_mode","pdf_dpi","pdf_min_kb","pdf_width","pdf_height","pdf_quality","pdf_gray"
];
const PANEL_IDS = ["epub-settings","pdf-settings"];

function loadPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    for (const id of SETTING_IDS) {
      if (!(id in (saved.values || {}))) continue;
      const element = $(id);
      if (element.type === "checkbox") element.checked = Boolean(saved.values[id]);
      else element.value = String(saved.values[id]);
    }
    for (const id of PANEL_IDS) {
      if (typeof saved.panels?.[id] === "boolean") $(id).open = saved.panels[id];
    }
  } catch (error) {
    console.warn("Could not restore Inkslim preferences:", error);
  }
}
function savePreferences() {
  const values = {};
  for (const id of SETTING_IDS) {
    const element = $(id);
    values[id] = element.type === "checkbox" ? element.checked : element.value;
  }
  const panels = Object.fromEntries(PANEL_IDS.map(id => [id,$(id).open]));
  try {
    localStorage.setItem(SETTINGS_KEY,JSON.stringify({values,panels}));
  } catch (error) {
    console.warn("Could not save Inkslim preferences:", error);
  }
}
loadPreferences();

function applyEpubPreset() {
  const preset = PRESETS[$("epub_preset").value];
  if (preset) {
    $("epub_width").value = preset.width;
    $("epub_height").value = preset.height;
    $("epub_quality").value = preset.quality;
    $("epub_min_kb").value = preset.min_kb;
    $("epub_gray").checked = preset.gray;
  }
  savePreferences();
}
$("epub_preset").addEventListener("change",applyEpubPreset);

for (const id of SETTING_IDS) {
  const element = $(id);
  element.addEventListener("input",() => {
    if (id.startsWith("epub_") && id !== "epub_preset") $("epub_preset").value = "custom";
    savePreferences();
  });
  element.addEventListener("change",() => {
    if (id.startsWith("epub_") && id !== "epub_preset") $("epub_preset").value = "custom";
    updatePdfControls();
    savePreferences();
  });
}
for (const id of PANEL_IDS) $(id).addEventListener("toggle",savePreferences);

function updatePdfControls() {
  const optimize = pdfMode.value === "optimize";
  pdfOptimizeFields.hidden = !optimize;
  pdfEreaderFields.hidden = optimize;
  pdfModeHint.textContent = optimize
    ? "Optimize PDF compresses embedded images while preserving selectable text and vector graphics. Digital signatures may no longer be valid, and some interactive features may change."
    : "E-reader mode renders every page as an image at the selected dimensions. Best for scanned books and comics; text-based PDFs usually get larger. Text selection, search, links, bookmarks, forms and signature validation are not retained.";
}
updatePdfControls();

drop.addEventListener("click",() => fileInput.click());
drop.addEventListener("keydown",(event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
for (const eventName of ["dragenter","dragover"]) {
  drop.addEventListener(eventName,(event) => {
    event.preventDefault();
    drop.classList.add("over");
  });
}
for (const eventName of ["dragleave","drop"]) {
  drop.addEventListener(eventName,(event) => {
    event.preventDefault();
    drop.classList.remove("over");
  });
}
drop.addEventListener("drop",(event) => addFiles(event.dataTransfer.files));
fileInput.addEventListener("change",() => {
  addFiles(fileInput.files);
  fileInput.value = "";
});

function fmt(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  return Math.max(1,Math.round(bytes / 1024)) + " KB";
}
function outName(name) {
  const extension = name.toLowerCase().endsWith(".pdf") ? ".pdf" : ".epub";
  return name.slice(0,-extension.length) + "-small" + extension;
}
const queue = [];
let busy = false;

function settingsForFile(file) {
  if (file.name.toLowerCase().endsWith(".pdf")) {
    return {
      width:$("pdf_width").value,
      height:$("pdf_height").value,
      quality:$("pdf_quality").value,
      min_kb:$("pdf_min_kb").value,
      gray:$("pdf_gray").checked ? "1" : "0",
      pdf_mode:pdfMode.value,
      pdf_dpi:$("pdf_dpi").value
    };
  }
  return {
    width:$("epub_width").value,
    height:$("epub_height").value,
    quality:$("epub_quality").value,
    min_kb:$("epub_min_kb").value,
    gray:$("epub_gray").checked ? "1" : "0"
  };
}
function updateHistory() {
  results.hidden = list.children.length === 0;
  clearButton.disabled = !list.querySelector('li[data-state="done"], li[data-state="error"]');
}
function clearHistory() {
  const finished = list.querySelectorAll('li[data-state="done"], li[data-state="error"]');
  for (const item of finished) {
    const link = item.querySelector("a.btn");
    if (link && link.href.startsWith("blob:")) URL.revokeObjectURL(link.href);
    item.remove();
  }
  updateHistory();
}
clearButton.addEventListener("click",clearHistory);

function addFiles(files) {
  for (const file of files) {
    const name = file.name.toLowerCase();
    if (!name.endsWith(".epub") && !name.endsWith(".pdf")) continue;
    const item = document.createElement("li");
    item.dataset.state = "queued";
    item.innerHTML = '<div class="name"></div><div class="meta"></div>';
    item.querySelector(".name").textContent = file.name;
    list.appendChild(item);
    // Capture settings when enqueued so mixed batches use the correct profile.
    queue.push({file,li:item,settings:settingsForFile(file)});
    setMeta(item,"Queued... (" + fmt(file.size) + ")",false,"queued");
  }
  updateHistory();
  next();
}
function setMeta(item,message,isError,state) {
  const meta = item.querySelector(".meta");
  if (!meta) return;
  meta.textContent = message;
  meta.classList.toggle("err",Boolean(isError));
  if (state) item.dataset.state = state;
  updateHistory();
}
function next() {
  if (busy || !queue.length) return;
  busy = true;
  const {file,li,settings} = queue.shift();
  const form = new FormData();
  form.append("file",file);
  for (const [key,value] of Object.entries(settings)) form.append(key,value);

  const bar = document.createElement("progress");
  bar.max = 100;
  bar.value = 0;
  li.appendChild(bar);
  setMeta(li,"Uploading...",false,"uploading");

  const xhr = new XMLHttpRequest();
  xhr.open("POST","shrink");
  xhr.responseType = "blob";
  xhr.upload.onprogress = (event) => {
    if (event.lengthComputable) bar.value = (event.loaded / event.total) * 100;
  };
  xhr.upload.onload = () => {
    bar.removeAttribute("value");
    setMeta(li,"Processing... this may take a moment.",false,"processing");
  };
  xhr.onload = async () => {
    bar.remove();
    if (xhr.status === 200) {
      const before = Number(xhr.getResponseHeader("X-Original-Size")) || file.size;
      const after = Number(xhr.getResponseHeader("X-New-Size")) || xhr.response.size;
      const percent = Math.round(100 - (after / before) * 100);
      const change = percent > 0 ? "(-" + percent + "%)" : percent < 0 ? "(+" + Math.abs(percent) + "% larger)" : "(same size)";
      setMeta(li,fmt(before) + " → " + fmt(after) + "  " + change,false,"done");
      const link = document.createElement("a");
      link.className = "btn";
      link.textContent = "Download";
      link.href = URL.createObjectURL(xhr.response);
      link.download = outName(file.name);
      li.appendChild(link);
    } else {
      let message = "Failed (" + xhr.status + ").";
      try { message = JSON.parse(await xhr.response.text()).error || message; } catch (_) {}
      setMeta(li,message,true,"error");
    }
    busy = false;
    updateHistory();
    next();
  };
  xhr.onerror = () => {
    bar.remove();
    setMeta(li,"Connection lost.",true,"error");
    busy = false;
    updateHistory();
    next();
  };
  xhr.send(form);
}
</script>
</body>
</html>
"""
