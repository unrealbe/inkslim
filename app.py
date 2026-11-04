
"""Small web app: drop an EPUB in and get a smaller EPUB back.

The actual processing is handled by epub-shrink.sh. Nothing is stored:
each upload is processed in a temporary directory and then deleted.
"""
import hmac
import io
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
TIMEOUT = int(os.environ.get("SHRINK_TIMEOUT", "840"))
SCRIPT = os.environ.get("SHRINK_SCRIPT", str(Path(__file__).with_name("epub-shrink.sh")))
AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")

app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024


@app.before_request
def require_login():
    """Optional password protection (set BASIC_AUTH_USER and BASIC_AUTH_PASS)."""
    if not AUTH_USER or request.path == "/healthz":
        return None
    auth = request.authorization
    if auth:
        ok_user = hmac.compare_digest((auth.username or "").encode(), AUTH_USER.encode())
        ok_pass = hmac.compare_digest((auth.password or "").encode(), AUTH_PASS.encode())
        if ok_user and ok_pass:
            return None
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="epub-shrink"'})


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error=f"File exceeds {MAX_MB} MB."), 413


def number(name, default, low, high):
    """Read a bounded number from the form, using a safe default."""
    try:
        value = int(request.form.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def safe_stem(filename):
    stem = Path(os.path.basename(filename or "boek.epub")).stem
    stem = re.sub(r"[\x00-\x1f\\/:*?\"<>|]", "_", stem).strip(" .") or "boek"
    return stem[:150]


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

    with tempfile.TemporaryDirectory(prefix="epubshrink-") as tmp:
        tmp = Path(tmp)
        # Keep these internal filenames: epub-shrink.sh expects this output name.
        src = tmp / "boek.epub"
        outdir = tmp / "out"
        outdir.mkdir()
        upload.save(src)
        if not zipfile.is_zipfile(src):
            return jsonify(error="This does not appear to be a valid EPUB."), 422

        cmd = ["bash", SCRIPT, "-w", str(width), "-h", str(height),
               "-q", str(quality), "-m", str(min_kb), "-o", str(outdir)]
        if gray:
            cmd.append("-g")
        cmd.append(str(src))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return jsonify(error="Processing took too long."), 504

        out = outdir / "boek-small.epub"
        if not out.is_file():
            if "Geen winst" in result.stdout:
                return jsonify(error="No reduction: this book cannot be made smaller with these settings."), 422
            if "geen geldige epub" in result.stderr or "MISLUKT" in result.stderr:
                return jsonify(error="This does not appear to be a valid EPUB."), 422
            app.logger.error("epub-shrink failed: %s", result.stderr[-500:])
            return jsonify(error="Processing failed."), 500

        original_size = src.stat().st_size
        new_size = out.stat().st_size
        data = io.BytesIO(out.read_bytes())

    response = send_file(
        data,
        mimetype="application/epub+zip",
        as_attachment=True,
        download_name=f"{safe_stem(upload.filename)}-small.epub",
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
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  #drop {
    border: 2px dashed var(--line); border-radius: 12px; padding: 36px 16px;
    text-align: center; cursor: pointer; transition: background .15s, border-color .15s;
  }
  #drop.over { background: var(--drop); border-color: var(--accent); }
  #drop strong { display: block; font-size: 1.1rem; }
  #drop span { color: var(--muted); font-size: .9rem; }
  details summary { cursor: pointer; font-weight: 600; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
  label { display: block; font-size: .85rem; color: var(--muted); }
  input[type=number], select {
    width: 100%; margin-top: 2px; padding: 8px; font: inherit; color: var(--text);
    background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 12px; color: var(--text); font-size: 1rem; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { padding: 12px 0; border-top: 1px solid var(--line); }
  li:first-child { border-top: 0; }
  .name { font-weight: 600; word-break: break-word; }
  .meta { color: var(--muted); font-size: .9rem; }
  .err { color: var(--err); }
  progress { width: 100%; height: 8px; margin-top: 6px; accent-color: var(--accent); }
  a.btn {
    display: inline-block; margin-top: 6px; padding: 8px 14px; border-radius: 8px;
    background: var(--accent); color: var(--accent-text); text-decoration: none; font-weight: 600;
  }
  .hint { color: var(--muted); font-size: .85rem; margin-top: 12px; }
  .history-header {
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px; margin-bottom: 8px; font-weight: 600;
  }
  #clear-history {
    border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px;
    background: var(--bg); color: var(--text); font: inherit; font-size: .85rem;
    cursor: pointer;
  }
  #clear-history:hover:not(:disabled) { border-color: var(--accent); }
  #clear-history:disabled { opacity: .5; cursor: not-allowed; }
</style>
</head>
<body>
<main>
  <h1>Inkslim</h1>
  <p class="sub">Drop one or more EPUB files here to get smaller versions for your e-reader.</p>

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
          <option value="custom">Custom values</option>
        </select>
      </label>
      <label>Maximum width (px)<input id="width" type="number" value="768" min="200" max="4000"></label>
      <label>Maximum height (px)<input id="height" type="number" value="1024" min="200" max="6000"></label>
      <label>JPEG quality (10-100)<input id="quality" type="number" value="75" min="10" max="100"></label>
      <label>Only process images larger than (kB)<input id="min_kb" type="number" value="50" min="1" max="10000"></label>
    </div>
    <label class="check"><input id="gray" type="checkbox" checked> Grayscale (smaller; no loss on a black-and-white screen)</label>
    <p class="hint">Images are only ever shrunk, never enlarged. Your original is never modified, and nothing is stored on the server.</p>
  </details>

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
  return Math.max(1, Math.round(bytes / 1024)) + " KB";
}

function outName(name) {
  return name.replace(/\\.epub$/i, "") + "-small.epub";
}

const queue = [];
let busy = false;

function updateHistory() {
  results.hidden = list.children.length === 0;
  clearButton.disabled = !list.querySelector('li[data-state="done"], li[data-state="error"]');
}

function clearHistory() {
  // Keep queued and running jobs visible; clear only completed or failed entries.
  const finishedItems = list.querySelectorAll('li[data-state="done"], li[data-state="error"]');
  for (const li of finishedItems) {
    const download = li.querySelector("a.btn");
    if (download && download.href.startsWith("blob:")) {
      URL.revokeObjectURL(download.href);
    }
    li.remove();
  }
  updateHistory();
}

clearButton.addEventListener("click", clearHistory);

function addFiles(files) {
  for (const f of files) {
    if (!/\\.epub$/i.test(f.name)) continue;
    const li = document.createElement("li");
    li.dataset.state = "queued";
    li.innerHTML = '<div class="name"></div><div class="meta"></div>';
    li.querySelector(".name").textContent = f.name;
    list.appendChild(li);
    queue.push({ file: f, li });
    setMeta(li, "Queued... (" + fmt(f.size) + ")", false, "queued");
  }
  updateHistory();
  next();
}

function setMeta(li, text, isErr, state) {
  const m = li.querySelector(".meta");
  if (!m) return;
  m.textContent = text;
  m.classList.toggle("err", !!isErr);
  if (state) li.dataset.state = state;
  updateHistory();
}

function next() {
  if (busy || !queue.length) return;
  busy = true;
  const { file, li } = queue.shift();
  const form = new FormData();
  form.append("file", file);
  for (const k of ["width", "height", "quality", "min_kb"]) form.append(k, $(k).value);
  form.append("gray", $("gray").checked ? "1" : "0");

  const bar = document.createElement("progress");
  bar.max = 100;
  bar.value = 0;
  li.appendChild(bar);
  setMeta(li, "Uploading...", false, "uploading");

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "shrink");
  xhr.responseType = "blob";
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) bar.value = (e.loaded / e.total) * 100;
  };
  xhr.upload.onload = () => {
    bar.removeAttribute("value");
    setMeta(li, "Processing... this may take a moment.", false, "processing");
  };
  xhr.onload = async () => {
    bar.remove();
    if (xhr.status === 200) {
      const before = Number(xhr.getResponseHeader("X-Original-Size")) || file.size;
      const after = Number(xhr.getResponseHeader("X-New-Size")) || xhr.response.size;
      const pct = Math.max(0, Math.round(100 - (after / before) * 100));
      setMeta(li, fmt(before) + " \\u2192 " + fmt(after) + "  (-" + pct + "%)", false, "done");

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
      setMeta(li, msg, true, "error");
    }
    busy = false;
    updateHistory();
    next();
  };
  xhr.onerror = () => {
    bar.remove();
    setMeta(li, "Connection lost.", true, "error");
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
