
"""Small web app: drop an EPUB in, get a smaller EPUB back.

The actual processing is handled by epub-shrink.sh. Nothing is stored:
every upload is processed in a temporary directory and then deleted.
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

from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
TIMEOUT = int(os.environ.get("SHRINK_TIMEOUT", "840"))
SCRIPT = os.environ.get(
    "SHRINK_SCRIPT",
    str(Path(__file__).with_name("epub-shrink.sh")),
)
AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
app.logger.setLevel(LOG_LEVEL)

app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024


@app.before_request
def require_login():
    """Optional password protection using BASIC_AUTH_USER and BASIC_AUTH_PASS."""
    if not AUTH_USER or request.path == "/healthz":
        return None

    auth = request.authorization
    if auth:
        ok_user = hmac.compare_digest(
            (auth.username or "").encode(),
            AUTH_USER.encode(),
        )
        ok_pass = hmac.compare_digest(
            (auth.password or "").encode(),
            AUTH_PASS.encode(),
        )
        if ok_user and ok_pass:
            return None

    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="inkslim"'},
    )


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error=f"File exceeds {MAX_MB} MB."), 413


def number(name, default, low, high):
    """Read a bounded integer from the form, with a safe default."""
    try:
        value = int(request.form.get(name, default))
    except (TypeError, ValueError):
        return default

    return max(low, min(high, value))


def safe_stem(filename):
    stem = Path(os.path.basename(filename or "book.epub")).stem
    stem = re.sub(r"[\x00-\x1f\\/:*?\"<>|]", "_", stem).strip(" .")
    return (stem or "book")[:150]


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

    if not upload.filename.lower().endswith(".epub"):
        return jsonify(error="Only .epub files are supported."), 400

    width = number("width", 768, 200, 4000)
    height = number("height", 1024, 200, 6000)
    quality = number("quality", 75, 10, 100)
    min_kb = number("min_kb", 50, 1, 10000)
    gray = request.form.get("gray") == "1"

    original_filename = upload.filename
    started = time.monotonic()

    app.logger.info(
        "Processing started: filename=%r width=%d height=%d "
        "quality=%d min_kb=%d grayscale=%s",
        original_filename,
        width,
        height,
        quality,
        min_kb,
        gray,
    )

    with tempfile.TemporaryDirectory(prefix="inkslim-") as tmp:
        tmp = Path(tmp)
        src = tmp / "book.epub"
        outdir = tmp / "out"
        outdir.mkdir()

        upload.save(src)
        original_size = src.stat().st_size

        if not zipfile.is_zipfile(src):
            app.logger.warning(
                "Rejected invalid EPUB: filename=%r size=%d",
                original_filename,
                original_size,
            )
            return jsonify(error="This does not appear to be a valid EPUB."), 422

        cmd = [
            "bash", SCRIPT,
            "-w", str(width),
            "-h", str(height),
            "-q", str(quality),
            "-m", str(min_kb),
            "-o", str(outdir),
        ]

        if gray:
            cmd.append("-g")

        cmd.append(str(src))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            app.logger.error(
                "Processing timed out after %.1f seconds: filename=%r",
                elapsed,
                original_filename,
            )
            return jsonify(error="Processing timed out."), 504

        out = outdir / "book-small.epub"

        if not out.is_file():
            elapsed = time.monotonic() - started

            if "Geen winst" in result.stdout:
                app.logger.info(
                    "No size reduction: filename=%r elapsed=%.1fs",
                    original_filename,
                    elapsed,
                )
                return jsonify(
                    error="No size reduction was possible with these settings."
                ), 422

            if (
                "geen geldige epub" in result.stderr.lower()
                or "MISLUKT" in result.stderr
            ):
                app.logger.warning(
                    "Invalid EPUB reported by processing script: filename=%r",
                    original_filename,
                )
                return jsonify(
                    error="This does not appear to be a valid EPUB."
                ), 422

            app.logger.error(
                "Processing failed: filename=%r elapsed=%.1fs "
                "returncode=%s stderr=%r",
                original_filename,
                elapsed,
                result.returncode,
                result.stderr[-500:],
            )
            return jsonify(error="Processing failed."), 500

        new_size = out.stat().st_size
        elapsed = time.monotonic() - started
        saved = original_size - new_size
        saved_pct = 100 * saved / original_size if original_size else 0

        app.logger.info(
            "Processing completed: filename=%r elapsed=%.1fs "
            "original_mb=%.2f new_mb=%.2f saved_mb=%.2f saved_pct=%.1f",
            original_filename,
            elapsed,
            original_size / 1048576,
            new_size / 1048576,
            saved / 1048576,
            saved_pct,
        )

        data = io.BytesIO(out.read_bytes())

    response = send_file(
        data,
        mimetype="application/epub+zip",
        as_attachment=True,
        download_name=f"{safe_stem(original_filename)}-small.epub",
    )
    response.headers["X-Original-Size"] = str(original_size)
    response.headers["X-New-Size"] = str(new_size)

    return response


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inkslim - EPUB optimizer</title>
<style>
  :root {
    --bg: #f6f5f1; --card: #ffffff; --text: #1d1d1b; --muted: #6b6a66;
    --line: #d9d7d0; --accent: #2f6f5e; --accent-text: #ffffff; --drop: #eef4f1;
    --err: #b3261e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #151615; --card: #1e201f; --text: #ecebe6; --muted: #9a9890;
      --line: #34372f; --accent: #5fb89f; --accent-text: #0e1512; --drop: #1b2924;
      --err: #f2a29b;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    padding: 24px 16px 48px;
  }
  main { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 4px; }
  p.sub { color: var(--muted); margin: 0 0 20px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 16px; margin-bottom: 16px;
  }
  #drop {
    border: 2px dashed var(--line); border-radius: 12px; padding: 36px 16px;
    text-align: center; cursor: pointer; transition: background .15s, border-color .15s;
  }
  #drop.over { background: var(--drop); border-color: var(--accent); }
  #drop strong { display: block; font-size: 1.1rem; }
  #drop span { color: var(--muted); font-size: .9rem; }
  details summary { cursor: pointer; font-weight: 600; }
  .grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px;
  }
  label { display: block; font-size: .85rem; color: var(--muted); }
  input[type=number], select {
    width: 100%; margin-top: 2px; padding: 8px; font: inherit; color: var(--text);
    background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  }
  .check {
    display: flex; align-items: center; gap: 8px; margin-top: 12px;
    color: var(--text); font-size: 1rem;
  }
  ul { list-style: none; margin: 0; padding: 0; }
  li { padding: 12px 0; border-top: 1px solid var(--line); }
  li:first-child { border-top: 0; }
  .name { font-weight: 600; word-break: break-word; }
  .meta { color: var(--muted); font-size: .9rem; }
  .err { color: var(--err); }
  progress { width: 100%; height: 8px; margin-top: 6px; accent-color: var(--accent); }
  a.btn {
    display: inline-block; margin-top: 6px; padding: 8px 14px; border-radius: 8px;
    background: var(--accent); color: var(--accent-text);
    text-decoration: none; font-weight: 600;
  }
  .hint { color: var(--muted); font-size: .85rem; margin-top: 12px; }
  .history-header {
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px; margin-bottom: 8px; font-weight: 600;
  }
  #clear-history {
    border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
    background: var(--bg); color: var(--text); font: inherit;
    font-size: .85rem; cursor: pointer;
  }
  #clear-history:hover { border-color: var(--accent); }
  #clear-history:disabled { opacity: .5; cursor: not-allowed; }
</style>
</head>
<body>
<main>
  <h1>Inkslim</h1>
  <p class="sub">Shrink EPUB files for your e-reader.</p>

  <div id="drop" class="card" tabindex="0" role="button" aria-label="Choose EPUB files">
    <strong>Drop your EPUB files here</strong>
    <span>or click to browse</span>
    <input id="file" type="file" accept=".epub,application/epub+zip" multiple hidden>
  </div>

  <details class="card">
    <summary>Settings</summary>
    <div class="grid">
      <label style="grid-column: 1 / -1">Preset
        <select id="preset">
          <option value="glo">Kobo Glo / black-and-white e-ink (768 x 1024, grayscale)</option>
          <option value="color">Colour screen, tablet or phone (1200 x 1600)</option>
          <option value="custom">Custom</option>
        </select>
      </label>
      <label>Maximum width (px)<input id="width" type="number" value="768" min="200" max="4000"></label>
      <label>Maximum height (px)<input id="height" type="number" value="1024" min="200" max="6000"></label>
      <label>JPEG quality (10-100)<input id="quality" type="number" value="75" min="10" max="100"></label>
      <label>Only process images larger than (kB)<input id="min_kb" type="number" value="50" min="1" max="10000"></label>
    </div>
    <label class="check"><input id="gray" type="checkbox" checked> Grayscale (smaller; no loss on a black-and-white screen)</label>
    <p class="hint">Images are only ever shrunk, never enlarged. Your original remains untouched and nothing is stored on the server.</p>
  </details>

  <div class="card" id="results" hidden>
    <div class="history-header">
      <span id="history-count">History (0)</span>
      <button id="clear-history" type="button">Clear history</button>
    </div>
    <ul id="list"></ul>
  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file"), list = $("list"), results = $("results");
const historyCount = $("history-count");
const clearButton = $("clear-history");

const PRESETS = {
  glo:   { width: 768,  height: 1024, quality: 75, min_kb: 50, gray: true },
  color: { width: 1200, height: 1600, quality: 80, min_kb: 50, gray: false },
};

$("preset").addEventListener("change", (e) => {
  const p = PRESETS[e.target.value];
  if (!p) return;
  for (const k of ["width", "height", "quality", "min_kb"]) $(k).value = p[k];
  $("gray").checked = p.gray;
});

for (const k of ["width", "height", "quality", "min_kb", "gray"]) {
  $(k).addEventListener("input", () => { $("preset").value = "custom"; });
}

drop.addEventListener("click", () => fileInput.click());
drop.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});

for (const ev of ["dragenter", "dragover"]) {
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("over");
  });
}

for (const ev of ["dragleave", "drop"]) {
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("over");
  });
}

drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
fileInput.addEventListener("change", () => {
  addFiles(fileInput.files);
  fileInput.value = "";
});

function fmt(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  return Math.max(1, Math.round(bytes / 1024)) + " kB";
}

function outName(name) {
  return name.replace(/\\.epub$/i, "") + "-small.epub";
}

const queue = [];
let busy = false;

function updateHistory() {
  historyCount.textContent = `History (${list.children.length})`;
  results.hidden = list.children.length === 0;
}

clearButton.addEventListener("click", () => {
  if (busy && !confirm("A file is being processed. Clear the history anyway?")) {
    return;
  }

  // Remove visible results. A running request is allowed to finish.
  list.replaceChildren();

  // Do not discard queued jobs; they will still be processed.
  updateHistory();
});

function addFiles(files) {
  for (const f of files) {
    if (!/\\.epub$/i.test(f.name)) continue;

    const li = document.createElement("li");
    li.innerHTML = '<div class="name"></div><div class="meta"></div>';
    li.querySelector(".name").textContent = f.name;
    list.appendChild(li);

    queue.push({ file: f, li });
    setMeta(li, "Queued... (" + fmt(f.size) + ")");
  }

  updateHistory();
  next();
}

function setMeta(li, text, isErr) {
  const m = li.querySelector(".meta");
  if (!m) return;
  m.textContent = text;
  m.classList.toggle("err", !!isErr);
}

function next() {
  if (busy || !queue.length) return;

  busy = true;
  const { file, li } = queue.shift();
  const form = new FormData();

  form.append("file", file);
  for (const k of ["width", "height", "quality", "min_kb"]) {
    form.append(k, $(k).value);
  }
  form.append("gray", $("gray").checked ? "1" : "0");

  const bar = document.createElement("progress");
  bar.max = 100;
  bar.value = 0;
  li.appendChild(bar);
  setMeta(li, "Uploading...");

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "shrink");
  xhr.responseType = "blob";

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) bar.value = (e.loaded / e.total) * 100;
  };

  xhr.upload.onload = () => {
    bar.removeAttribute("value");
    setMeta(li, "Shrinking... this may take a moment.");
  };

  xhr.onload = async () => {
    bar.remove();

    if (xhr.status === 200) {
      const before = Number(xhr.getResponseHeader("X-Original-Size")) || file.size;
      const after = Number(xhr.getResponseHeader("X-New-Size")) || xhr.response.size;
      const pct = Math.max(0, Math.round(100 - (after / before) * 100));

      setMeta(li, fmt(before) + " → " + fmt(after) + "  (-" + pct + "%)");

      const a = document.createElement("a");
      a.className = "btn";
      a.textContent = "Download";
      a.href = URL.createObjectURL(xhr.response);
      a.download = outName(file.name);
      li.appendChild(a);
    } else {
      let msg = "Failed (" + xhr.status + ").";
      try {
        msg = JSON.parse(await xhr.response.text()).error || msg;
      } catch (_) {}
      setMeta(li, msg, true);
    }

    busy = false;
    updateHistory();
    next();
  };

  xhr.onerror = () => {
    bar.remove();
    setMeta(li, "Connection lost.", true);
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
